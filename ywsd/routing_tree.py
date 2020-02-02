from enum import Enum
from typing import List, Dict, Tuple
import os.path
import uuid

from ywsd.objects import Extension, CallgroupRank, Yate


class RoutingTree:
    def __init__(self, source, target, settings):
        self.source_extension = source
        self.target_extension = target
        self.source = None
        self.target = None
        self._settings = settings

        self.routing_result = None
        self.new_routing_cache_content = {}

    async def discover_tree(self, db_connection):
        await self._load_source_and_target(db_connection)
        visitor = RoutingTreeDiscoveryVisitor(self.target, [self.source_extension])
        await visitor.discover_tree(db_connection)
        return visitor

    def calculate_routing(self, local_yate: int, yates_dict: Dict[int, Yate]) -> \
            Tuple['IntermediateRoutingResult', Dict[str, 'IntermediateRoutingResult']]:
        self._calculate_main_routing(local_yate, yates_dict)
        self._provide_ringback()
        self._populate_eventphone_parameters()

        return self.routing_result, self.new_routing_cache_content

    def _calculate_main_routing(self, local_yate, yates_dict):
        visitor = YateRoutingGenerationVisitor(self, local_yate, yates_dict)
        result = visitor.calculate_routing()
        self.routing_result = result
        self.new_routing_cache_content = visitor.get_routing_cache_content()

    def _provide_ringback(self):
        if self.target.ringback is not None:
            ringback_path = os.path.join(self._settings.RINGBACK_TOP_DIRECTORY, self.target.ringback)
            if os.path.isfile(ringback_path):
                if self.routing_result.is_simple:
                    # we need to convert routing result into a simple fork
                    self.routing_result = IntermediateRoutingResult(
                        target=CallTarget("fork", self.routing_result.target.parameters),
                        fork_targets=[
                            self._make_ringback_target(ringback_path),
                            self.routing_result.target
                        ]
                    )
                else:
                    # if the routing target is already a callfork, just prepend the ringback target to the first group
                    self.routing_result.fork_targets.insert(0, self._make_ringback_target(ringback_path))

    def _calculate_eventphone_parameters(self):
        # push parameters here like faked-caller-id or caller-language
        return {}

    def _populate_eventphone_parameters(self):
        eventphone_parameters = self._calculate_eventphone_parameters()
        self.routing_result.target.parameters.update(eventphone_parameters)
        for entry in self.new_routing_cache_content:
            entry.update(eventphone_parameters)

    @staticmethod
    def _make_ringback_target(path):
        return CallTarget(
            "wave/play/" + path,
            {
                "fork.calltype": "persistent",
                "fork.autoring": "true",
                "fork.automessage": "call.progress",
            })

    async def _load_source_and_target(self, db_connection):
        self.source = await Extension.load_extension(self.source_extension, db_connection)
        self.target = await Extension.load_extension(self.target_extension, db_connection)


class RoutingTreeDiscoveryVisitor:
    def __init__(self, root_node, excluded_targets, max_depth=10):
        self._root_node = root_node
        self._excluded_targets = set(excluded_targets)
        self._discovery_log = []
        self._max_depth = max_depth
        self._failed = False
        self._pruned = False

    @property
    def failed(self):
        return self._failed

    @property
    def pruned(self):
        return self._pruned

    def get_log(self):
        return self._discovery_log

    def _log(self, msg, level=None):
        self._discovery_log.append(msg)

    async def discover_tree(self, db_connection):
        await self._visit(self._root_node, 0, list(self._excluded_targets), db_connection)

    async def _visit(self, node: Extension, depth: int, path_extensions: list, db_connection):
        if depth >= self._max_depth:
            self._log("Routing aborted due to depth limit at {}".format(node))
            self._failed = True
            return

        path_extensions_local = path_extensions.copy()
        path_extensions_local.append(node.extension)

        if node.type != Extension.Type.EXTERNAL and node.forwarding_mode != Extension.ForwardingMode.DISABLED:
            await node.load_forwarding_extension(db_connection)
        if node.type in (Extension.Type.GROUP, Extension.Type.MULTIRING) \
                and (node.forwarding_mode != Extension.ForwardingMode.ENABLED or node.forwarding_delay > 0):
            # we discover group members if there is no immediate forward
            await node.populate_callgroup_ranks(db_connection)
        # now we visit the populated children if they haven't been already discovered
        if node.forwarding_extension is not None:
            fwd = node.forwarding_extension
            if fwd.extension not in path_extensions_local:
                await self._visit(fwd, depth+1, path_extensions_local, db_connection)
            else:
                self._pruned = True
                self._log("Discovery aborted for forward to {}, was already present. Discovery state: {}\n"
                          "Disabling Forward".format(fwd, path_extensions_local))
                node.forwarding_mode = Extension.ForwardingMode.DISABLED
        for callgroup_rank in node.callgroup_ranks:
            for member in callgroup_rank.members:
                # do not discover inactive members
                if not member.active:
                    continue
                ext = member.extension
                if ext.extension not in path_extensions_local:
                    await self._visit(ext, depth+1, path_extensions_local, db_connection)
                else:
                    self._pruned = True
                    self._log("Discovery aborted for {} in {}, was already present. Discovery state: {}\n"
                              "Temporarily disable membership for this routing."
                              .format(ext, callgroup_rank, path_extensions_local))
                    member.active = False


class CallTarget:
    target = ""
    parameters = {}

    def __init__(self, target, parameters=None):
        self.target = target
        self.parameters = parameters if parameters is not None else {}

    def __repr__(self):
        return "<CallTarget {}, params={}>".format(self.target, self.parameters)


class IntermediateRoutingResult:
    class Type(Enum):
        SIMPLE = 0
        FORK = 1

    def __init__(self, target: CallTarget = None, fork_targets: List['CallTarget'] = None):
        if fork_targets is not None:
            self.type = IntermediateRoutingResult.Type.FORK
            self.fork_targets = fork_targets
            self.target = target
        else:
            self.type = IntermediateRoutingResult.Type.SIMPLE
            self.target = target
            self.fork_targets = []

    def __repr__(self):
        fork_targets_str = "\n\t\t".join([repr(targ) for targ in self.fork_targets])
        return "<IntermediateRoutingResult\n\ttarget={}\n\tfork_targets=\n\t\t{}\n>".format(self.target,
                                                                                            fork_targets_str)

    @property
    def is_simple(self):
        return self.type == IntermediateRoutingResult.Type.SIMPLE


class YateRoutingGenerationVisitor:
    def __init__(self, routing_tree: RoutingTree, local_yate_id: int, yates_dict: Dict[int, Yate]):
        self._routing_tree = routing_tree
        self._local_yate_id = local_yate_id
        self._yates_dict = yates_dict
        self._lateroute_cache: Dict[str, IntermediateRoutingResult] = {}
        self._x_eventphone_id = uuid.uuid4().hex

    def get_routing_cache_content(self):
        return self._lateroute_cache

    def _make_intermediate_result(self, target: CallTarget, fork_targets=None):
        return IntermediateRoutingResult(target=target, fork_targets=fork_targets)

    def _make_calltarget(self, target: str, parameters: dict = None):
        # write default parameters into the calltarget
        if parameters is None:
            parameters = {}
        parameters["x_eventphone_id"] = self._x_eventphone_id
        return CallTarget(target=target, parameters=parameters)

    def _cache_intermediate_result(self, result: IntermediateRoutingResult):
        if not result.is_simple:
            self._lateroute_cache[result.target.target] = result

    def calculate_routing(self):
        return self._visit_for_route_calculation(self._routing_tree.target, [])

    def _visit_for_route_calculation(self, node: Extension, path: list) -> IntermediateRoutingResult:
        local_path = path.copy()
        local_path.append(node.id)

        # first we check if this node has an immediate forward. If yes, we defer routing there.
        if node.immediate_forward:
            return self._visit_for_route_calculation(node.forwarding_extension, local_path)

        if YateRoutingGenerationVisitor.node_has_simple_routing(node):
            print("Node {} has simple routing".format(node))
            return self._make_intermediate_result(target=self.generate_simple_routing_target(node))
        else:
            print("Node {} requires complex routing".format(node))
            # this will require a fork

            # go through the callgroup ranks to issue the groups of the fork
            fork_targets = []
            accumulated_delay = 0
            for rank in node.callgroup_ranks:
                if fork_targets:
                    # this is not the first rank, so we need to generate a separator
                    if rank.mode == CallgroupRank.Mode.DROP:
                        separator = "|drop={}".format(rank.delay)
                        accumulated_delay += rank.delay
                    elif rank.mode == CallgroupRank.Mode.NEXT:
                        separator = "|next={}".format(rank.delay)
                        accumulated_delay += rank.delay
                    else:
                        separator = "|"
                    if accumulated_delay >= node.forwarding_delay:
                        # all of those will not be called, as the forward takes effect now
                        break
                    # Do not generate default params on pseudo targets
                    fork_targets.append(CallTarget(separator))
                for member in rank.members:
                    # do not route inactive members
                    if not member.active:
                        continue
                    member_route = self._visit_for_route_calculation(member.extension, local_path)
                    if member.type.is_special_calltype:
                        member_route.target.params["fork.calltype"] = member.type.fork_calltype
                    # please note that we ignore the member modes for the time being
                    fork_targets.append(member_route.target)
                    self._cache_intermediate_result(member_route)

            # if this is a MULTIRING, the extension itself needs to be part of the first group
            if node.type == Extension.Type.MULTIRING:
                fork_targets.insert(0, self.generate_simple_routing_target(node))
            # we might need to issue a delayed forward

            if node.forwarding_mode == Extension.ForwardingMode.ENABLED:
                # this is forward with a delay. We want to know how to route there...
                forwarding_route = self._visit_for_route_calculation(node.forwarding_extension, local_path)
                fwd_delay = node.forwarding_delay - accumulated_delay
                fork_targets.append(CallTarget("|drop={}".format(fwd_delay)))
                fork_targets.append(forwarding_route.target)
                self._cache_intermediate_result(forwarding_route)
            
            return self._make_intermediate_result(
                fork_targets=fork_targets, target=self._make_calltarget(self.generate_deferred_routestring(local_path)))

    @staticmethod
    def node_has_simple_routing(node: Extension):
        if node.type == Extension.Type.EXTERNAL:
            return True
        # if the node is set to immediate forward, it will be ignored for routing and we calculate the routing for
        # the forwarded node instead
        if node.immediate_forward:
            return YateRoutingGenerationVisitor.node_has_simple_routing(node.forwarding_extension)

        if node.type == Extension.Type.SIMPLE:
            if node.forwarding_mode == Extension.ForwardingMode.DISABLED:
                return True
            # Forwarding with delay or ON_BUSY requires a callfork
            return False
        # multiring is simple, if there are no active multiring participants configured
        if node.type == Extension.Type.MULTIRING:
            if node.has_active_group_members:
                return False
            # ok, nothing to multiring, so look at the forwarding mode
            if node.forwarding_mode == Extension.ForwardingMode.DISABLED:
                return True
            # Forwarding with delay or ON_BUSY requires a callfork
            return False
        # groups might have a simple routing if they have exactly one participant. We will ignore this possibility
        # for the moment being. We could do this by introducing an optimizer stage that reshapes the tree :P
        return False

    def generate_simple_routing_target(self, node: Extension):
        if node.yate_id == self._local_yate_id:
            return self._make_calltarget("lateroute/stage2-{}".format(node.extension))
        else:
            return self._make_calltarget("sip/sip:{}@{}"
                                         .format(node.extension, self._yates_dict[node.yate_id].hostname),
                                         {"oconnection_id": self._yates_dict[node.yate_id].voip_listener})

    def generate_deferred_routestring(self, path):
        return "lateroute/" + self.generate_node_route_string(path)

    def generate_node_route_string(self, path):
        joined_path = "-".join(map(str, path))
        return "stage1-{}-{}".format(self._x_eventphone_id, joined_path)
