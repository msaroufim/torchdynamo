import collections
import contextlib
import dataclasses
import functools
import itertools
import logging
import os
from typing import Any
from typing import Dict
from typing import List

import numpy as np
import torch

from . import config
from . import dependencies
from . import ir
from .codegen.triton_template import template_codegen
from .dependencies import MemoryDep
from .dependencies import StarDep
from .sizevars import SimplifyIndexing
from .virtualized import V

template_kernels = [ir.Convolution]

log = logging.getLogger(__name__)

INDUCTOR_SCHEDULER_GRAPH = bool(os.environ.get("INDUCTOR_SCHEDULER_GRAPH", None) == "1")


def cmp(a, b):
    return int(a > b) - int(a < b)


def should_use_template(node: ir.ExternKernel):
    return (
        type(node) in template_kernels
        and ir.is_triton(node.get_device())
        # TODO(jansel): extend this to other kernels
        and config.triton.convolution != "aten"
    )


class OutputNode:
    def __init__(self, dep):
        self.unmet_dependencies = {dep}
        self.inverse_users = []

    def is_reduction(self):
        return False

    def get_alias_names(self):
        return ()

    def get_name(self):
        return "OUTPUT"

    __repr__ = get_name


class BaseSchedulerNode:
    def __init__(self, scheduler: "Scheduler", node: ir.Buffer):
        self.scheduler = scheduler
        self.node = node
        self.users = None
        self.inverse_users = []
        self.set_read_writes(node.get_read_writes())

    def __repr__(self):
        return f"{type(self).__name__}(name={self.get_name()!r})"

    def update_mutated_names(self, renames: Dict[str, str]):
        self.set_read_writes(self.read_writes.rename(renames))

    def add_mutation_dep(self, name):
        self.set_read_writes(self.read_writes.with_read(name))

    def set_users(self, users: List["NodeUser"]):
        # deduplicate
        result = {}
        for use in users:
            if id(use.node) in result:
                result[id(use.node)] = NodeUser(
                    use.node, result[id(use.node)].can_inplace and use.can_inplace
                )
            else:
                result[id(use.node)] = use
        self.users = list(result.values())

    def get_aliases(self):
        return self.node.get_alias_names()

    def get_mutations(self):
        return self.node.get_mutation_names()

    def set_read_writes(self, rw):
        self.read_writes = rw
        self.unmet_dependencies = self.read_writes.reads
        self.prune_deps()

    def prune_deps(self):
        self.unmet_dependencies = {
            dep
            for dep in self.unmet_dependencies
            if dep.name not in self.scheduler.available_buffer_names
        }

    def get_name(self):
        return self.node.get_name()

    def get_device(self):
        return self.node.get_device()

    def is_reduction(self):
        return False

    def can_inplace(self, read_dep: dependencies.MemoryDep):
        return False

    def allocate(self):
        if self.node.should_allocate() or should_use_template(self.node):
            # if self.node should allocate or
            # if self.node is generated by TritonKernelTemplates
            # because Triton kernel could not allocate tensor itself
            V.graph.wrapper_code.codegen_allocation(self.node)

    def can_free(self):
        for use in self.users:
            if isinstance(use.node, OutputNode):
                return False
            name = use.get_name()
            if name not in self.scheduler.available_buffer_names:
                return False
        return True

    def get_priority(self):
        """Controls the order this node will be executed in, higher runs first"""
        raise NotImplementedError()


class ExternKernelSchedulerNode(BaseSchedulerNode):
    def __init__(self, scheduler: "Scheduler", node: ir.ExternKernel, group_fn):
        super().__init__(scheduler, node)
        if should_use_template(node):
            (self._sizes, self._stride) = node.get_group_stride()
            self.group = (node.get_device(), group_fn(self._sizes))
            self.set_read_writes(node.get_read_writes())

    def can_remove_buffer(self, **kwargs):
        return False

    def update_dep_type(self):
        assert isinstance(self.node, ir.Convolution)
        assert len(self.read_writes.writes) == 1
        write = self.read_writes.writes.pop()
        if isinstance(write, StarDep):
            name = write.name
            canonicalized_index, canonicalized_size = self.node.canonicalize()
            new_dep = MemoryDep(name, canonicalized_index, canonicalized_size)
            self.read_writes.writes.add(new_dep)
        else:
            self.read_writes.writes.add(write)

    def mark_fusable(self, broadcast_after_reduce=False):
        self.scheduler.fusable_deps.update(self.read_writes.writes)
        if broadcast_after_reduce and self.is_reduction():
            self.scheduler.fusable_deps.update(
                w.broadcast_extend_sizes(self._sizes[-1])
                for w in self.read_writes.writes
            )

    def get_ranges(self):
        return self._sizes

    def run(self, codegen_extern_call):
        log.info(f"RUN EXTERN {self.get_name()}")
        self.allocate()
        self.scheduler.run_count += 1
        self.scheduler.pending_buffer_names.add(self.get_name())
        if not should_use_template(self.node):
            # TemplateKernelSchedulerNode will be added to scheduler in template_codegen
            self.scheduler.kernels.append(self.node)
        codegen_extern_call(self)

    def get_priority(self):
        return 100


class NopKernelSchedulerNode(BaseSchedulerNode):
    def can_remove_buffer(self, **kwargs):
        return False

    def run(self):
        log.info(f"RUN NOP {self.get_name()}")
        self.allocate()
        self.scheduler.run_count += 1
        self.scheduler.pending_buffer_names.add(self.get_name())

    def get_priority(self):
        return 200


def pick_loop_order(stride_lengths, sizes, priority_idx=[]):
    """
    A heuristic to decide loop iteration orders.  This has not been well
    tuned and may be something we should autotune.
    """

    @functools.cmp_to_key
    def index_cmp(a, b):
        if sizes[a] == 1 or sizes[b] == 1:
            # 1-sizes don't matter, just move them to the end
            return cmp(sizes[a] == 1, sizes[b] == 1)

        a_first = np.logical_or(
            stride_lengths[:, b] == 0, stride_lengths[:, a] < stride_lengths[:, b]
        ).all()
        b_first = np.logical_or(
            stride_lengths[:, a] == 0, stride_lengths[:, a] > stride_lengths[:, b]
        ).all()

        if a_first and not b_first:
            return -1
        if b_first and not a_first:
            return 1

        # otherwise contiguous
        return cmp(b, a)

    order = list(reversed(range(stride_lengths.shape[1])))
    if len(priority_idx) > 0:
        # if we have priority node, only use that node's order
        stride_lengths = stride_lengths[priority_idx]
    if config.pick_loop_orders:
        order.sort(key=index_cmp)
    return order


class SchedulerNode(BaseSchedulerNode):
    def __init__(self, scheduler: "Scheduler", node: ir.ComputedBuffer, group_fn):
        super().__init__(scheduler, node)
        (
            self._sizes,
            self._body,
        ) = node.simplify_reorder_and_tile()

        self.group = (node.get_device(), group_fn(self._sizes))
        self.set_read_writes(
            dependencies.extract_read_writes(self._body, *self._sizes, normalize=True)
        )

    def can_remove_buffer(self, check_group):
        if (
            self.is_reduction()
            and len(self.users) == 1
            and isinstance(self.users[0].node, SchedulerNode)
            and len(self.users[0].node.unmet_dependencies) == 1
        ):
            user = self.users[0].node
            if not check_group(user):
                return False
            dep = next(iter(user.unmet_dependencies))
            writes = self.read_writes.writes
            if self._sizes[-1] != 1:
                writes = set(writes)
                writes.update(
                    [w.broadcast_extend_sizes(self._sizes[-1]) for w in writes]
                )
            # this will get fused into us, so we don't need to keep the buffer
            return not user.is_reduction() and dep in writes
        return False

    def mark_fusable(self, broadcast_after_reduce=False):
        self.scheduler.fusable_deps.update(self.read_writes.writes)
        if self.is_reduction():
            # reduction has last (reduced) dim in its sizes, and some
            # downstream dependencies get confused by it
            self.scheduler.fusable_deps.update(
                w.strip_last_size() for w in self.read_writes.writes
            )
            # reduction not on the last dim swaps the sizes, and downstream
            # dependencies expect unswapped
            # TODO swapping sizes doesn't work, leads to
            # File "/scratch/ngimel/work/repos/torchdynamo/torchinductor/sizevars.py", line 130, in guard_equals
            # if len(right.free_symbols) < len(left.free_symbols):
            # AttributeError: 'int' object has no attribute 'free_symbols'
            # even though memory dep looks correct

            # self.scheduler.fusable_deps.update(
            #     w.maybe_swap_sizes() for w in self.read_writes.writes
            # )

    def get_ranges(self):
        return self._sizes

    def is_reduction(self):
        return bool(self.node.data.get_reduction_type())

    def allocate(self):
        if (
            not self.node.should_allocate()
            or self.node.get_alias_names()
            or self.node.get_mutation_names()
        ):
            return super().allocate()

        if config.inplace_buffers:
            for read in self.read_writes.reads:
                input_node: BaseSchedulerNode = self.scheduler.name_to_node.get(
                    read.name
                )
                if input_node and V.graph.wrapper_code.can_reuse(input_node):
                    remaining_uses = [
                        x
                        for x in input_node.users
                        if x.node.get_name()
                        not in self.scheduler.available_buffer_names
                    ]
                    if (
                        len(remaining_uses) == 1
                        and remaining_uses[0].can_inplace
                        and remaining_uses[0].node is self
                    ):
                        V.graph.wrapper_code.codegen_inplace_reuse(
                            input_node.node, self.node
                        )
                        V.kernel.args.make_inplace(
                            input_node.get_name(), self.get_name()
                        )
                        return
        super().allocate()

    def run(self, *index_vars):
        log.info(f"RUN {self.get_name()}")
        self.allocate()
        self.scheduler.run_count += 1
        sizes = self._sizes
        assert sum(map(len, sizes)) == sum(map(len, index_vars))
        var_ranges = dict(
            zip(
                itertools.chain.from_iterable(index_vars),
                itertools.chain.from_iterable(sizes),
            )
        )
        with V.set_ops_handler(
            SimplifyIndexing(V.get_ops_handler(), var_ranges)
        ), V.kernel.set_current_node(self):
            self._body(*index_vars)
        self.scheduler.pending_buffer_names.add(self.get_name())

    def can_inplace(self, read_dep: dependencies.MemoryDep):
        if self.node.get_alias_names():
            return False
        if len(self.read_writes.writes) == 1 and hasattr(read_dep, "index"):
            write_dep = next(iter(self.read_writes.writes))
            return read_dep.index == write_dep.index and read_dep.size == write_dep.size
        return False

    def get_priority(self):
        if self.is_reduction():
            return len(self.group)
        else:
            return len(self.group) - 1


@dataclasses.dataclass
class SchedulerNodeBox:
    """Allow us to invalidate a blocked node"""

    value: SchedulerNode

    def __bool__(self):
        return self.value is not None

    def pop(self) -> SchedulerNode:
        assert self
        val = self.value
        self.value = None
        return val

    def peek(self) -> SchedulerNode:
        return self.value


class BlockedNodes:
    def __init__(self):
        super().__init__()
        self.name_to_nodes = collections.defaultdict(list)
        self.dep_to_nodes = collections.defaultdict(list)

    def add(self, node: SchedulerNode):
        box = SchedulerNodeBox(node)
        for dep in node.unmet_dependencies:
            self.dep_to_nodes[dep].append(box)
        for name in {dep.name for dep in node.unmet_dependencies}:
            self.name_to_nodes[name].append(box)

    def pop_name(self, name):
        return [x.pop() for x in self.name_to_nodes.pop(name, []) if x]

    def pop_fusable(self, deps, group):
        assert isinstance(deps, set)
        result = []
        for dep in deps:
            self.dep_to_nodes[dep] = [x for x in self.dep_to_nodes[dep] if x]
            for box in self.dep_to_nodes[dep]:
                if (
                    len(box.peek().unmet_dependencies - deps) == 0
                    and box.peek().group == group
                ):
                    result.append(box.pop())
        return result


@dataclasses.dataclass
class NodeUser:
    node: BaseSchedulerNode
    can_inplace: bool = False

    def get_name(self):
        return self.node.get_name()


def get_fake_func(name):
    def func1(*args):
        return 0

    func1.__name__ = name
    return func1


def create_fx_from_buffers(nodes, fname, print_graph=False):
    """
    Draw a graph in fname.svg.
    nodes is a list of SchedulerNode objects.
    """

    from functorch._src.partitioners import draw_graph
    from torch.fx.graph_module import GraphModule
    from torch.fx.passes.shape_prop import TensorMetadata
    from torch.fx.passes.tools_common import legalize_graph

    func_dict = {}
    name_to_fx_node = {}
    graph = torch.fx.Graph()
    first_node = None

    # create call_function node for each Buffer and Kernel
    for snode in nodes:
        node = snode.node
        name = node.get_name()
        node_type = str(type(node)).split(".")[-1].replace("'>", "")

        if node_type in func_dict:
            fake_f = func_dict[node_type]
        else:
            fake_f = get_fake_func(node_type)
            func_dict[node_type] = fake_f
        fx_node = graph.call_function(fake_f, args=(), kwargs=None)
        fx_node.name = name

        # gather meta data
        dtype = None
        if isinstance(node, ir.ComputedBuffer):
            dtype = node.data.dtype

        try:
            stride = node.get_stride()
        except AttributeError:
            stride = None

        layout = type(node.layout)

        if isinstance(snode, NopKernelSchedulerNode):
            group = "nop"
        elif isinstance(snode, ExternKernelSchedulerNode):
            if should_use_template(node):
                group = snode.group[1]
            else:
                group = "extern"
        else:  # SchedulerNode
            group = snode.group[1]

        metadata = TensorMetadata(group, dtype, False, stride, layout, None, None)
        fx_node.meta["tensor_meta"] = metadata

        name_to_fx_node[name] = fx_node
        if first_node is None:
            first_node = fx_node

    # create edges between nodes
    for snode in nodes:
        node = snode.node
        name = node.get_name()
        deps = node.get_reads()
        fx_node = name_to_fx_node[name]

        new_args = []
        for dep in deps:
            if dep.name in name_to_fx_node:
                dep_node = name_to_fx_node[dep.name]
            else:
                with graph.inserting_before(first_node):
                    dep_node = graph.placeholder(dep.name)
                    name_to_fx_node[dep.name] = dep_node
            new_args.append(dep_node)

        fx_node.args = tuple(new_args)

    outputs = []
    for _, v in name_to_fx_node.items():
        if len(v.users) == 0:
            outputs.append(v)
    graph.output(outputs[0] if len(outputs) == 1 else tuple(outputs))

    if print_graph:
        print(graph)
    print("starting creating module")
    gm = GraphModule({}, graph)
    graph = legalize_graph(gm)
    gm.graph.lint()
    print("starting drawing")
    draw_graph(gm, fname, clear_meta=False)


class Scheduler:
    def __init__(self, nodes):
        super(Scheduler, self).__init__()
        self.backends = {}
        self.current_device = None
        # runnable_groups maps node group to priority
        # we use self.runnable_groups.most_common() to implement a priority queue
        self.runnable_groups = collections.Counter()
        # runnable_nodes  maps node group to nodes
        self.runnable_nodes: Dict[Any, SchedulerNode] = collections.defaultdict(list)
        self.runnable_extern_kernels = collections.deque()
        self.blocked_nodes = BlockedNodes()
        self.run_count = 0
        self.nodes = []
        self.kernels = []
        self.available_buffer_names = {
            *V.graph.graph_inputs.keys(),
            *V.graph.constants.keys(),
        }
        self.pending_buffer_names = set()
        self.check_can_free = set()
        self.fusable_deps = set()
        for node in nodes:
            if node.is_no_op():
                self.nodes.append(NopKernelSchedulerNode(self, node))
            elif isinstance(node, ir.ComputedBuffer):
                group_fn = self.get_backend(node.get_device()).group_fn
                self.nodes.append(SchedulerNode(self, node, group_fn))
            elif isinstance(node, ir.ExternKernel):
                group_fn = None
                if should_use_template(node):
                    if isinstance(node, ir.Convolution):
                        group_fn = self.get_backend(node.get_device()).group_fn_NHW_C
                    else:
                        group_fn = self.get_backend(node.get_device()).group_fn
                self.nodes.append(ExternKernelSchedulerNode(self, node, group_fn))
            else:
                assert False, node
        self.name_to_node = {node.get_name(): node for node in self.nodes}

        if INDUCTOR_SCHEDULER_GRAPH:

            try:
                from functorch._src.aot_autograd import get_graph_being_compiled

                graph_name = get_graph_being_compiled()
            except ImportError:
                logging.warning(
                    "Could not get graph name from `get_graph_being_compiled` \
                    in functorch, use 'model' as default"
                )
                graph_name = "model"

            create_fx_from_buffers(self.nodes, graph_name, print_graph=True)

        # some new constants could have been created above
        self.available_buffer_names.update(V.graph.constants.keys())

        # we handle mutation by renaming modified versions of the same
        # buffer in the dependency graph to prevent cycles.
        # mutation_renames: tracks the current name for a given buffer
        #                   (changed once per mutation)
        self.mutation_real_name = {}
        # mutation_real_name: maps back to the original name for codegen
        self.mutation_renames = {}

        self.compute_users()
        self.dead_node_elimination()
        self.enqueue(self.nodes)

    def compute_users(self):
        name_to_users = collections.defaultdict(list)

        # handle aliasing by using python aliasing in name_to_users
        # if foo aliases bar then we will make name_to_users["foo"] point
        # to the same python list as name_to_users["bar"]
        for node1 in self.nodes:
            node1_name = node1.get_name()
            for node2_name in node1.get_aliases():
                if node1_name in name_to_users and node2_name in name_to_users:
                    # merge the two
                    list1 = name_to_users[node1_name]
                    list2 = name_to_users[node2_name]
                    combined = list1 + list2
                    for key in name_to_users.keys():
                        if name_to_users[key] is list1 or name_to_users[key] is list2:
                            name_to_users[key] = combined
                elif node1_name in name_to_users:
                    name_to_users[node2_name] = name_to_users[node1_name]
                else:
                    name_to_users[node1_name] = name_to_users[node2_name]

        def rename(n):
            if n in self.mutation_renames:
                return rename(self.mutation_renames[n])
            return n

        def dep_closure(node_name):
            reachable_names = {node_name}
            node = self.name_to_node[node_name]
            write_dep = list(node.read_writes.writes)[0]
            for read_dep in node.read_writes.reads:
                if (
                    read_dep.name in self.name_to_node
                    and read_dep.index == write_dep.index
                    and read_dep.size == write_dep.size
                ):
                    reachable_names.update(dep_closure(read_dep.name))
            return reachable_names

        def add_user(used_by_name, user_node, can_inplace=False):
            name_to_users[rename(used_by_name)].append(NodeUser(user_node, can_inplace))

        for node in self.nodes:
            # a node will mutate either 0 or 1 buffers
            for alt_name in node.get_mutations():
                alt_name = rename(alt_name)
                # this node must run after the prior writer
                add_user(alt_name, node)
                node.add_mutation_dep(alt_name)
                for other_node in name_to_users[alt_name]:
                    # this node must run after all prior readers
                    other_name = rename(other_node.get_name())
                    known_dep_node_names = dep_closure(node.get_name())
                    if other_name not in known_dep_node_names:
                        # If this node alreay directly or indirectly depends on other_node,
                        # we don't need to insert an extra StarDep.
                        node.add_mutation_dep(other_name)
                        add_user(other_name, node)

            # add normal non-mutation dependencies
            for read in node.read_writes.reads:
                add_user(read.name, node, node.can_inplace(read))

            node.update_mutated_names(self.mutation_renames)

            # update our renaming scheme for the next iteration
            for alt_name in node.get_mutations():
                self.mutation_renames[rename(alt_name)] = node.get_name()
                self.mutation_renames[alt_name] = node.get_name()
                self.mutation_real_name[node.get_name()] = self.mutation_real_name.get(
                    alt_name, alt_name
                )

        # make sure outputs aren't dead-code-eliminated
        for node in V.graph.graph_outputs:
            if not isinstance(node, ir.NoneAsConstantBuffer):
                name = node.get_name()
                add_user(node.get_name(), OutputNode(StarDep(name)))

        # make sure input mutation isn't dead-code-eliminated
        for name in self.mutation_renames:
            if name in V.graph.graph_inputs:
                add_user(name, OutputNode(StarDep(name)))
                V.graph.mutated_inputs.add(name)

        # copy users information onto the nodes
        for node in self.nodes:
            node.set_users(name_to_users[node.get_name()])

        # populate inverse_users
        for node in self.nodes:
            for user in node.users:
                user.node.inverse_users.append(node)

    def dead_node_elimination(self):
        updated_nodes = []
        for node in self.nodes:
            if node.users:
                updated_nodes.append(node)
            else:
                # dead code
                log.debug("removed dead node: %s", node.get_name())
                V.graph.removed_buffers.add(node.get_name())
        self.nodes = updated_nodes

    def enqueue(self, node):
        if isinstance(node, (tuple, list)):
            for n in node:
                self.enqueue(n)
            return

        assert isinstance(node, BaseSchedulerNode)
        if node.unmet_dependencies:
            self.blocked_nodes.add(node)
        else:
            if isinstance(node, ExternKernelSchedulerNode):
                self.runnable_extern_kernels.append(node)
            elif isinstance(node, NopKernelSchedulerNode):
                node.run()  # just schedule nop kernels eagerly
            else:  # SchedulerNode
                self.runnable_nodes[node.group].append(node)
                old_priority, old_count = self.runnable_groups.get(node.group, (0, 0))
                self.runnable_groups[node.group] = (
                    max(old_priority, node.get_priority()),
                    old_count + 1,
                )

    def remove_kernel_local_buffers(self):
        # If all the uses of this buffer are also in self.pending_buffer_names,
        # it means the buffer becomes kernel-local after fusion
        for name in self.pending_buffer_names:
            if name in V.kernel.must_keep_buffers:
                continue
            node = self.name_to_node[name]
            is_live = any(
                [
                    (user.get_name() not in self.pending_buffer_names)
                    for user in node.users
                ]
            )
            if not is_live:
                self.remove_buffer(name)

    def remove_buffer(self, name):
        # Assign a special value instead of deleting the entry
        # because we still rely on output_buffers's length to
        # generate unique arg name.
        V.kernel.args.output_buffers[name] = "REMOVED"
        V.graph.removed_buffers.add(name)

    def barrier(self):
        """
        Mark all pending_buffer_names as available and enqueue any nodes
        that became runnable.
        """
        if config.debug and (self.fusable_deps or self.pending_buffer_names):

            def gc(d):
                return {k: v for k, v in d.items() if any(v)}

            log.info(f"blocked names: {gc(self.blocked_nodes.dep_to_nodes)}")
            log.info(f"blocked deps: {gc(self.blocked_nodes.name_to_nodes)}")
            log.info(f"new fusable_deps: {self.fusable_deps}")

        while self.pending_buffer_names:
            self.available_buffer_names.update(self.pending_buffer_names)
            nodes_to_add = []
            for name in self.pending_buffer_names:
                self.check_can_free.update(self.pending_buffer_names)
                for node in self.blocked_nodes.pop_name(name):
                    node.prune_deps()
                    nodes_to_add.append(node)
            self.pending_buffer_names.clear()
            self.enqueue(nodes_to_add)

    def maybe_free_buffers(self):
        # perhaps there are some unused buffers we can free
        for done_name in self.check_can_free:
            done_node = self.name_to_node[done_name]
            for node in done_node.inverse_users:
                name = node.get_name()
                if node.can_free() and name:
                    if name in self.mutation_renames:
                        continue
                    if name in self.mutation_real_name:
                        name = self.mutation_real_name[name]
                        if name in self.name_to_node:
                            V.graph.wrapper_code.codegen_free(
                                self.name_to_node[name].node
                            )
                    else:
                        V.graph.wrapper_code.codegen_free(node.node)
        self.check_can_free.clear()

    def kernel(self, kernel):
        log.info("NEW KERNEL")
        self.fusable_deps.clear()
        self.kernels.append(kernel)

        @contextlib.contextmanager
        def ctx():
            with kernel:
                yield kernel

        return ctx()

    def iter_runnable_groups(self):
        while self.runnable_groups or self.runnable_extern_kernels:
            if self.runnable_extern_kernels:
                runnable_extern_kernel = self.runnable_extern_kernels.popleft()
                try:
                    self.current_device = runnable_extern_kernel.get_device()
                except AttributeError:
                    # 'MultiOutputLayout' object has no attribute 'device'
                    pass
                runnable_extern_kernel.run(self.codegen_extern_call)
            else:
                group, priority = self.runnable_groups.most_common(1)[0]
                del self.runnable_groups[group]
                yield group
        assert not self.runnable_nodes
        assert len(self.nodes) == self.run_count

    def iter_fixed_point(self):
        """
        Keep yielding until self.run_count converges
        """
        prior_run_count = -1
        while prior_run_count != self.run_count:
            prior_run_count = self.run_count
            yield

    def pop_group(self, group_without_device):
        group = (self.current_device, tuple(group_without_device))
        while group in self.runnable_nodes:
            if group in self.runnable_groups:
                del self.runnable_groups[group]
            yield from self.runnable_nodes.pop(group)
        if self.fusable_deps:
            fusable = True
            while fusable:
                # keep poping fusable nodes as their depdencies are satisfied
                fusable = self.blocked_nodes.pop_fusable(self.fusable_deps, group)
                yield from fusable

    def pop_groups(self, groups):
        keep_going = True
        while keep_going:
            keep_going = False
            for group in groups:
                for node in self.pop_group(group):
                    keep_going = True
                    yield node

    def flush(self):
        for backend in self.backends.values():
            backend.flush()

    def codegen_extern_call(self, scheduler_node: ExternKernelSchedulerNode):
        assert isinstance(scheduler_node, ExternKernelSchedulerNode)
        node = scheduler_node.node
        self.flush()
        if should_use_template(node):
            template_codegen(self, scheduler_node)
        else:
            node.codegen(V.graph.wrapper_code)
            self.barrier()
            self.maybe_free_buffers()

    def create_backend(self, device: torch.device):
        V.graph.device_types.add(device.type)
        if device.type == "cpu":
            from .codegen.cpp import CppScheduling

            return CppScheduling(self)
        else:
            from .codegen.triton import TritonScheduling

            return TritonScheduling(self)

    def get_backend(self, device: torch.device):
        if device not in self.backends:
            self.backends[device] = self.create_backend(device)
        return self.backends[device]

    def codegen(self):
        for device, group in self.iter_runnable_groups():
            if device != self.current_device:
                self.flush()
                self.current_device = device
            self.get_backend(device).codegen(*group)
        self.flush()
