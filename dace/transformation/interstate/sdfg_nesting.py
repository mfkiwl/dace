""" SDFG nesting transformation. """

from copy import deepcopy as dc
import itertools
import networkx as nx
from typing import Dict, List, Set

from dace import data as dt, memlet, sdfg as sd, Memlet, EmptyMemlet
from dace.graph import nodes, nxutil
from dace.graph.graph import MultiConnectorEdge, SubgraphView
from dace.sdfg import SDFG, SDFGState
from dace.transformation import pattern_matching
from dace.properties import make_properties, Property


@make_properties
class InlineSDFG(pattern_matching.Transformation):
    """ Inlines a single-state nested SDFG into a top-level SDFG.

        In particular, the steps taken are:

        1. All transient arrays become transients of the parent
        2. If a source/sink node is one of the inputs/outputs:
          a. Remove it
          b. Reconnect through external edges (map/accessnode)
          c. Replace and reoffset memlets with external data descriptor
        3. If other nodes carry the names of inputs/outputs:
          a. Replace data with external data descriptor
          b. Replace and reoffset memlets with external data descriptor
        4. If source/sink node is not connected to a source/destination, and
           the nested SDFG is in a scope, connect to scope with empty memlets
        5. Remove all unused external inputs/output memlet paths
        6. Remove isolated nodes resulting from previous step

    """

    _nested_sdfg = nodes.NestedSDFG('_', sd.SDFG('_'), set(), set())

    @staticmethod
    def annotates_memlets():
        return True

    @staticmethod
    def expressions():
        # Matches anything
        return [nxutil.node_path_graph(InlineSDFG._nested_sdfg)]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        nested_sdfg = graph.nodes()[candidate[InlineSDFG._nested_sdfg]]
        if len(nested_sdfg.sdfg.nodes()) != 1:
            return False

        # Ensure every connector has one incoming/outgoing edge
        in_connectors = set()
        out_connectors = set()
        for edge in graph.in_edges(nested_sdfg):
            if edge.dst_conn in in_connectors:
                return False
            in_connectors.add(edge.dst_conn)
        for edge in graph.out_edges(nested_sdfg):
            if edge.src_conn in out_connectors:
                return False
            out_connectors.add(edge.src_conn)

        return True

    @staticmethod
    def match_to_str(graph, candidate):
        return graph.label

    def _modify_memlet(self, internal_memlet: Memlet, external_memlet: Memlet):
        """ Unsqueezes and offsets a memlet, as per the semantics of nested
            SDFGs.
            :param internal_memlet: The internal memlet (inside nested SDFG)
                                    before modification.
            :param internal_memlet: The external memlet before modification.
            :return: Offset Memlet to set on the resulting graph.
        """
        result = dc(internal_memlet)
        result.data = external_memlet.data

        shape = external_memlet.subset.size()
        if len(internal_memlet.subset) < len(external_memlet.subset):
            ones = [i for i, d in enumerate(shape) if d == 1]

            # Special case: If internal memlet is a range of size 1 with (0,0,1),
            #               ignore it when unsqueezing
            if (len(internal_memlet.subset) == 1
                    and (internal_memlet.subset[0] == (0, 0, 1)
                         or internal_memlet.subset[0] == 0)):
                to_unsqueeze = ones[1:]
            else:
                to_unsqueeze = ones

            result.subset.unsqueeze(to_unsqueeze)
        elif len(internal_memlet.subset) > len(external_memlet.subset):
            raise ValueError(
                'Unexpected extra dimensions in internal memlet '
                'while inlining SDFG.\nExternal memlet: %s\n'
                'Internal memlet: %s' % (external_memlet, internal_memlet))

        result.subset.offset(external_memlet.subset, False)

        # TODO: Offset rest of memlet according to other_subset
        if external_memlet.other_subset is not None:
            raise NotImplementedError

        return result

    def _remove_edge_path(self,
                          state: SDFGState,
                          edge_map: Dict[str, MultiConnectorEdge],
                          unused: Set[str],
                          reverse: bool = False) -> List[MultiConnectorEdge]:
        """ Remove all edges along a path, until memlet tree contains siblings
            that should not be removed. Removes resulting isolated nodes as
            well. Operates in place.
            :param state: The state in which to remove edges.
            :param edge_map: Mapping from identifier to edge, used as a
                             predicate for removal.
            :param unused: Set of edge identifiers to remove.
            :param reverse: If False, removes forward in path, otherwise
                            backward.
            :return: List of edges from removed nodes at the path's end.
        """

        if reverse:
            edge_func = lambda e: state.out_edges(e.src)
            edge_pred = lambda pedge, e: e.src_conn == pedge.src_conn
        else:
            edge_func = lambda e: state.in_edges(e.dst)
            edge_pred = lambda pedge, e: e.dst_conn == pedge.dst_conn

        result = []

        for identifier, edge in edge_map.items():
            if identifier in unused:
                path = state.memlet_path(edge)
                pedge = None
                for pedge in (reversed(path) if reverse else path):
                    # If there are no other edges, it is safe to remove
                    if len([
                            e for e in edge_func(pedge) if edge_pred(pedge, e)
                    ]) == 1:
                        # Remove connectors as well
                        state.remove_edge_and_connectors(pedge)
                    else:
                        break
                else:  # Reached terminus without breaking, remove external node
                    if pedge is not None:
                        node = pedge.src if reverse else pedge.dst

                        # Keep track of edges on the other end of these nodes,
                        # they will be used to reconnect to first/last
                        # occurrence of access nodes in the inlined subgraph.
                        if reverse:
                            result.extend(state.in_edges(node))
                        else:
                            result.extend(state.out_edges(node))

                        state.remove_node(node)

        return result

    def apply(self, sdfg):
        state: SDFGState = sdfg.nodes()[self.state_id]
        nsdfg_node = state.nodes()[self.subgraph[InlineSDFG._nested_sdfg]]
        nsdfg: SDFG = nsdfg_node.sdfg
        nstate: SDFGState = nsdfg.nodes()[0]

        nsdfg_scope_entry = state.entry_node(nsdfg_node)
        nsdfg_scope_exit = (state.exit_nodes(nsdfg_scope_entry)
                            if nsdfg_scope_entry is not None else None)

        #######################################################
        # Collect and update top-level SDFG metadata

        # Find original source/destination edges (there is only one edge per
        # connector, according to match)
        inputs: Dict[str, MultiConnectorEdge] = {}
        outputs: Dict[str, MultiConnectorEdge] = {}
        input_set: Dict[str, str] = {}
        output_set: Dict[str, str] = {}
        for e in state.in_edges(nsdfg_node):
            inputs[e.dst_conn] = e
            input_set[e.data.data] = e.dst_conn
        for e in state.out_edges(nsdfg_node):
            outputs[e.src_conn] = e
            output_set[e.data.data] = e.src_conn

        # All transients become transients of the parent (if data already
        # exists, find new name)
        # Mapping from nested transient name to top-level name
        transients: Dict[str, str] = {}
        for node in nstate.nodes():
            if isinstance(node, nodes.AccessNode):
                datadesc = nsdfg.arrays[node.data]
                if node.data not in transients and datadesc.transient:
                    name = sdfg.add_datadesc(
                        '%s_%s' % (nsdfg.label, node.data),
                        datadesc,
                        find_new_name=True)
                    transients[node.data] = name

        # Collect nodes to add to top-level graph
        new_incoming_edges: Dict[nodes.Node, MultiConnectorEdge] = {}
        new_outgoing_edges: Dict[nodes.Node, MultiConnectorEdge] = {}

        source_accesses = set()
        sink_accesses = set()
        for node in nstate.source_nodes():
            if (isinstance(node, nodes.AccessNode)
                    and node.data not in transients):
                new_incoming_edges[node] = inputs[node.data]
                source_accesses.add(node)
        for node in nstate.sink_nodes():
            if (isinstance(node, nodes.AccessNode)
                    and node.data not in transients):
                new_outgoing_edges[node] = outputs[node.data]
                sink_accesses.add(node)

        #######################################################
        # Add nested SDFG into top-level SDFG

        # Add nested nodes into original state
        subgraph = SubgraphView(nstate, [
            n for n in nstate.nodes()
            if n not in (source_accesses | sink_accesses)
        ])
        state.add_nodes_from(subgraph.nodes())
        for edge in subgraph.edges():
            state.add_edge(edge.src, edge.src_conn, edge.dst, edge.dst_conn,
                           edge.data)

        #######################################################
        # Replace data on inlined SDFG nodes/edges

        # Replace data names with their top-level counterparts
        repldict = {}
        repldict.update(transients)
        repldict.update({
            k: v.data.data
            for k, v in itertools.chain(inputs.items(), outputs.items())
        })
        for match, replacement in repldict.items():
            sd.replace(subgraph, match, replacement)

        #######################################################
        # Reconnect inlined SDFG

        # If a source/sink node is one of the inputs/outputs, reconnect it,
        # replacing memlets in outgoing/incoming paths
        modified_edges = set()
        modified_edges |= self._modify_memlet_path(new_incoming_edges, nstate,
                                                   state, True)
        modified_edges |= self._modify_memlet_path(new_outgoing_edges, nstate,
                                                   state, False)

        # Modify all other internal edges pertaining to input/output nodes
        for node in subgraph.nodes():
            if isinstance(node, nodes.AccessNode):
                if node.data in input_set:
                    for edge in state.out_edges(node):
                        if edge not in modified_edges:
                            edge._data = self._modify_memlet(
                                edge.data, inputs[input_set[node.data]].data)
                # Note that data can both be in the input and output sets
                if node.data in output_set:
                    for edge in state.in_edges(node):
                        if edge not in modified_edges:
                            edge._data = self._modify_memlet(
                                edge.data, outputs[output_set[node.data]].data)

        # If source/sink node is not connected to a source/destination access
        # node, and the nested SDFG is in a scope, connect to scope with empty
        # memlets
        if nsdfg_scope_entry is not None:
            for node in subgraph.nodes():
                if state.in_degree(node) == 0:
                    state.add_edge(nsdfg_scope_entry, None, node, None,
                                   EmptyMemlet())
                if state.out_degree(node) == 0:
                    state.add_edge(node, None, nsdfg_scope_exit, None,
                                   EmptyMemlet())

        # Replace nested SDFG parents with new SDFG
        for node in nstate.nodes():
            if isinstance(node, nodes.NestedSDFG):
                node.sdfg.parent = state
                node.sdfg.parent_sdfg = sdfg

        # Remove all unused external inputs/output memlet paths, as well as
        # resulting isolated nodes
        removed_in_edges = self._remove_edge_path(
            state, inputs, set(inputs.keys()) - source_accesses, reverse=True)
        removed_out_edges = self._remove_edge_path(
            state, outputs, set(outputs.keys()) - sink_accesses, reverse=False)

        # Re-add in/out edges to first/last nodes in subgraph
        order = [
            x for x in nx.topological_sort(nstate._nx)
            if isinstance(x, nodes.AccessNode)
        ]
        for edge in removed_in_edges:
            # Find first access node that refers to this edge
            node = next(n for n in order if n.data == edge.data.data)
            state.add_edge(edge.src, edge.src_conn, node, edge.dst_conn,
                           edge.data)
        for edge in removed_out_edges:
            # Find last access node that refers to this edge
            node = next(n for n in reversed(order) if n.data == edge.data.data)
            state.add_edge(node, edge.src_conn, edge.dst, edge.dst_conn,
                           edge.data)

        #######################################################
        # Remove nested SDFG node
        state.remove_node(nsdfg_node)
        '''
        to_reconnect_inp = set()
        to_reconnect_out = set()

        torename = {}
        torename.update({k: v[2].data for k, v in inputs.items()})
        torename.update({k: v[2].data for k, v in outputs.items()})

        # Add SDFG nodes to top-level SDFG
        state = nsdfg.nodes()[0]
        # Keep a backup of the topological sorted order of the access nodes,
        order = [
            x for x in reversed(list(nx.topological_sort(state._nx)))
            if isinstance(x, nodes.AccessNode)
        ]
        for node in state.nodes():
            # Data access nodes
            if isinstance(node, nodes.AccessNode):
                # External node
                if node.data in inputs or node.data in outputs:
                    continue
                # Internal node (e.g., transient)
                if node.data not in torename:
                    name = node.data
                    # Name already exists
                    if name in sdfg.arrays:
                        name = '%s_%s' % (nsdfg.label, node.data)
                        i = 0
                        while name in sdfg.arrays:
                            name = '%s_%s_%d' % (nsdfg.label, node.data, i)
                            i += 1
                    # Add transient
                    sdfg.arrays[name] = nsdfg.arrays[node.data]
                    # Rename all internal uses
                    torename[node.data] = name
            # Set all parents of nested SDFG nodes in the inlined SDFG to their
            # new parent
            elif isinstance(node, nodes.NestedSDFG):
                node.sdfg.parent = graph
                node.sdfg.parent_sdfg = sdfg

            graph.add_node(node)
            to_reconnect_inp.add(node)
            to_reconnect_out.add(node)

        # TODO: Confirm that the following is always correct
        # Add Scalars of the nested SDFG to the parent
        for name, arr in nsdfg.arrays.items():
            if isinstance(arr, dt.Scalar) and name not in sdfg.arrays:
                sdfg.arrays[name] = arr

        # Reconnect edges to their original source
        for e in state.edges():
            if isinstance(e.src, nodes.AccessNode) and e.src.data in inputs:
                cnode, cconn, cmemlet = inputs[e.src.data]
                # Connect to source node instead
                newmemlet = self._modify_memlet(e.data, cmemlet)
                graph.add_edge(cnode, cconn, e.dst, e.dst_conn, newmemlet)
                try:
                    to_reconnect_inp.remove(e.dst)
                except KeyError:
                    # TODO: Benign?
                    pass
            elif isinstance(e.dst, nodes.AccessNode) and e.dst.data in outputs:
                cnode, cconn, cmemlet = outputs[e.dst.data]
                newmemlet = self._modify_memlet(e.data, cmemlet)
                if state.out_edges(e.dst):
                    # Connector is written in a non-sink access node
                    graph.add_edge(e.src, e.src_conn, e.dst, e.dst_conn,
                                   newmemlet)
                    # Check if there is another sink-node for the connector.
                    n = next((x for x in order if x.label == e.dst.label),
                             None)
                    if not state.out_edges(n):
                        continue
                    else:
                        # Connector is ONLY written in a non-sink access node,
                        # through the exit node to the true output access node.
                        e._src = e._dst
                        e._src_conn = e._dst_conn
                        # Remove wcr
                        newmemlet = dc(newmemlet)
                        newmemlet.wcr = None
                        newmemlet.other_subset = dc(newmemlet.subset)
                        for _, _, dst, _, memlet in graph.out_edges(cnode):
                            if isinstance(dst, nodes.AccessNode
                                          ) and memlet.data == cmemlet.data:
                                memlet.wcr = None
                # Connect to destination node instead
                graph.add_edge(e.src, e.src_conn, cnode, cconn, newmemlet)
                try:
                    to_reconnect_out.remove(e.src)
                except KeyError:
                    # TODO: Benign?
                    pass
            elif e.data.data in torename:
                if e.data.data in inputs:
                    newmemlet = self._modify_memlet(e.data,
                                                    inputs[e.data.data][2])
                elif e.data.data in outputs:
                    newmemlet = self._modify_memlet(e.data,
                                                    outputs[e.data.data][2])
                else:
                    # Rename data
                    cdata = torename[e.data.data]
                    newmemlet = dc(e.data)
                    newmemlet.data = cdata

                graph.add_edge(e.src, e.src_conn, e.dst, e.dst_conn, newmemlet)
            else:
                # Do nothing
                graph.add_edge(e.src, e.src_conn, e.dst, e.dst_conn, e.data)

        # Rename all access nodes
        for node in state.nodes():
            if isinstance(node, nodes.AccessNode) and node.data in torename:
                node.data = torename[node.data]

        # If in scope, reconnect all source and sink nodes with empty memlets
        scope_node = graph.scope_dict()[nsdfg_node]
        if scope_node is not None:
            scope_exit = graph.exit_nodes(scope_node)[0]
            for node in state.source_nodes():
                if node in to_reconnect_inp:
                    graph.add_edge(scope_node, None, node, None, EmptyMemlet())
                    try:
                        to_reconnect_inp.remove(node)
                    except KeyError:
                        # TODO: Benign?
                        pass
            for node in state.sink_nodes():
                if node in to_reconnect_out:
                    graph.add_edge(node, None, scope_exit, None, EmptyMemlet())
                    try:
                        to_reconnect_out.remove(node)
                    except KeyError:
                        # TODO: Benign?
                        pass

        # Remove the nested SDFG node
        graph.remove_node(nsdfg_node)

        # Remove input/output nodes from top-level graph if not connected to
        # any internal node
        for node, _, _ in list(inputs.values()) + list(outputs.values()):
            if len(graph.all_edges(node)) == 0:
                graph.remove_node(node)
        '''

    def _modify_memlet_path(self,
                            new_edges: Dict[nodes.Node, MultiConnectorEdge],
                            nstate: SDFGState, state: SDFGState,
                            inputs: bool) -> Set[MultiConnectorEdge]:
        """ Modifies memlet paths in an inlined SDFG. Returns set of modified
            edges.
        """
        result = set()
        for node, top_edge in new_edges.items():
            inner_edges = (nstate.out_edges(node)
                           if inputs else nstate.in_edges(node))
            for inner_edge in inner_edges:
                new_memlet = self._modify_memlet(inner_edge.data,
                                                 top_edge.data)
                if inputs:
                    new_edge = state.add_edge(top_edge.src, top_edge.src_conn,
                                              inner_edge.dst,
                                              inner_edge.dst_conn, new_memlet)
                    mtree = state.memlet_tree(new_edge)
                    mtree = mtree[mtree.index(new_edge) + 1:]
                else:
                    new_edge = state.add_edge(
                        inner_edge.src, inner_edge.src_conn, top_edge.dst,
                        top_edge.dst_conn, new_memlet)
                    mtree = state.memlet_tree(new_edge)
                    mtree = mtree[:mtree.index(new_edge)]

                # Modify all memlets going forward/backward
                for tree_edge in mtree:
                    result.add(tree_edge)
                    tree_edge._data = self._modify_memlet(
                        tree_edge.data, top_edge.data)
        return result


@make_properties
class NestSDFG(pattern_matching.Transformation):
    """ Implements SDFG Nesting, taking an SDFG as an input and creating a
        nested SDFG node from it. """

    promote_global_trans = Property(
        dtype=bool,
        default=False,
        desc="Promotes transients to be allocated once")

    @staticmethod
    def annotates_memlets():
        return True

    @staticmethod
    def expressions():
        # Matches anything
        return [nx.DiGraph()]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True

    @staticmethod
    def match_to_str(graph, candidate):
        return graph.label

    def apply(self, sdfg):

        outer_sdfg = sdfg
        nested_sdfg = dc(sdfg)

        outer_sdfg.arrays.clear()
        outer_sdfg.remove_nodes_from(outer_sdfg.nodes())

        inputs = {}
        outputs = {}
        transients = {}

        for state in nested_sdfg.nodes():
            #  Input and output nodes are added as input and output nodes of the nested SDFG
            for node in state.nodes():
                if (isinstance(node, nodes.AccessNode)
                        and not node.desc(nested_sdfg).transient):
                    if (state.out_degree(node) > 0):  # input node
                        arrname = node.data
                        if arrname not in inputs:
                            arrobj = nested_sdfg.arrays[arrname]
                            nested_sdfg.arrays[arrname + '_in'] = arrobj
                            outer_sdfg.arrays[arrname] = dc(arrobj)
                            inputs[arrname] = arrname + '_in'
                        node_data_name = arrname + '_in'
                    if (state.in_degree(node) > 0):  # output node
                        arrname = node.data
                        if arrname not in outputs:
                            arrobj = nested_sdfg.arrays[arrname]
                            nested_sdfg.arrays[arrname + '_out'] = arrobj
                            if arrname not in inputs:
                                outer_sdfg.arrays[arrname] = dc(arrobj)
                            outputs[arrname] = arrname + '_out'
                        node_data_name = arrname + '_out'
                    node.data = node_data_name

            if self.promote_global_trans:
                scope_dict = state.scope_dict()
                for node in state.nodes():
                    if (isinstance(node, nodes.AccessNode)
                            and node.desc(nested_sdfg).transient):

                        arrname = node.data
                        if arrname not in transients and not scope_dict[node]:
                            arrobj = nested_sdfg.arrays[arrname]
                            nested_sdfg.arrays[arrname + '_out'] = arrobj
                            outer_sdfg.arrays[arrname] = dc(arrobj)
                            transients[arrname] = arrname + '_out'
                        node.data = arrname + '_out'

        for arrname in inputs.keys():
            nested_sdfg.arrays.pop(arrname)
        for arrname in outputs.keys():
            nested_sdfg.arrays.pop(arrname, None)
        for oldarrname, newarrname in transients.items():
            nested_sdfg.arrays.pop(oldarrname)
            nested_sdfg.arrays[newarrname].transient = False
        outputs.update(transients)

        # Update memlets
        for state in nested_sdfg.nodes():
            for _, edge in enumerate(state.edges()):
                _, _, _, _, mem = edge
                src = state.memlet_path(edge)[0].src
                dst = state.memlet_path(edge)[-1].dst
                if isinstance(src, nodes.AccessNode):
                    if (mem.data in inputs.keys()
                            and src.data == inputs[mem.data]):
                        mem.data = inputs[mem.data]
                    elif (mem.data in outputs.keys()
                          and src.data == outputs[mem.data]):
                        mem.data = outputs[mem.data]
                elif (isinstance(dst, nodes.AccessNode)
                      and mem.data in outputs.keys()
                      and dst.data == outputs[mem.data]):
                    mem.data = outputs[mem.data]

        outer_state = outer_sdfg.add_state(outer_sdfg.label)

        nested_node = outer_state.add_nested_sdfg(nested_sdfg, outer_sdfg,
                                                  inputs.values(),
                                                  outputs.values())
        for key, val in inputs.items():
            arrnode = outer_state.add_read(key)
            outer_state.add_edge(
                arrnode, None, nested_node, val,
                memlet.Memlet.from_array(key, arrnode.desc(outer_sdfg)))
        for key, val in outputs.items():
            arrnode = outer_state.add_write(key)
            outer_state.add_edge(
                nested_node, val, arrnode, None,
                memlet.Memlet.from_array(key, arrnode.desc(outer_sdfg)))


pattern_matching.Transformation.register_stateflow_pattern(NestSDFG)
pattern_matching.Transformation.register_pattern(InlineSDFG)
