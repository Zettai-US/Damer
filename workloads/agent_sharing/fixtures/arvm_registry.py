"""Executable control-path prototype for accepted, read-only object plans.
Native mappings are exercised separately by cxl_bench.c. This module does not
claim to interpose on arbitrary tools, manage GPU pointers, or persist state.
"""
from dataclasses import dataclass
from collections import defaultdict, deque
from threading import RLock


@dataclass(frozen=True)
class Handle:
    name: str
    generation: int


@dataclass
class Object:
    handle: Handle
    size: int
    scopes: frozenset
    roots: int = 1
    pins: int = 0
    retiring: bool = False
    live: bool = True


@dataclass(frozen=True)
class Task:
    name: str
    predecessors: tuple
    inputs: tuple
    output_bound: int


@dataclass(frozen=True)
class Contract:
    epoch: int
    scope: str
    order: tuple
    pins: frozenset
    reservation_bytes: int


class Registry:
    def __init__(self, capacity):
        self.lock = RLock()
        self.capacity = capacity
        self.reserved = 0
        self.epoch = 0
        self.generations = defaultdict(int)
        self.objects = {}
        self.active = {}
        self.next_lease = 0

    def publish(self, name, size, scopes):
        if size < 0 or not scopes: raise ValueError('invalid object')
        with self.lock:
            self.generations[name] += 1
            h = Handle(name, self.generations[name])
            self.objects[h] = Object(h, size, frozenset(scopes))
            self.epoch += 1
            return h

    def validate(self, handle, scope):
        obj = self.objects.get(handle)
        if obj is None or not obj.live or obj.retiring:
            raise ValueError('stale or retiring object')
        if scope not in obj.scopes: raise PermissionError('unauthorized reader')
        return obj

    def admit(self, contract):
        with self.lock:
            if contract.epoch != self.epoch: raise ValueError('stale plan epoch')
            objects = [self.validate(h, contract.scope) for h in contract.pins]
            if contract.reservation_bytes < 0: raise ValueError('negative reservation')
            if self.reserved + contract.reservation_bytes > self.capacity:
                raise MemoryError('insufficient capacity')
            for o in objects: o.pins += 1
            self.reserved += contract.reservation_bytes
            self.next_lease += 1
            self.active[self.next_lease] = contract
            return self.next_lease

    def finish(self, lease):
        with self.lock:
            c = self.active.pop(lease)
            for h in c.pins: self.objects[h].pins -= 1
            self.reserved -= c.reservation_bytes

    def retire(self, handle):
        with self.lock:
            o = self.objects[handle]
            o.retiring = True
            self.epoch += 1

    def drop_root(self, handle):
        with self.lock:
            o = self.objects[handle]
            if not o.roots: raise ValueError('root already dropped')
            o.roots -= 1

    def reclaim(self, handle):
        with self.lock:
            o = self.objects[handle]
            if not o.live: raise ValueError('already reclaimed')
            if not o.retiring or o.pins or o.roots:
                raise RuntimeError('object remains reachable')
            o.live = False


class Compiler:
    """Full DAG validation plus incremental updates of output bounds.

    The incremental path preserves topology and input handles. Structural
    replanning deliberately takes the full path in this minimal prototype.
    The output reservation is a conservative sum, not an optimal liveness bound.
    """
    def __init__(self, registry):
        self.registry = registry
        self.tasks = {}
        self.contract = None

    def compile(self, tasks, scope):
        task_map = {t.name: t for t in tasks}
        if len(task_map) != len(tasks): raise ValueError('duplicate task name')
        indegree = {}; children = defaultdict(list); inputs = set(); total = 0
        for task in tasks:
            if task.output_bound < 0: raise ValueError('negative output bound')
            if len(set(task.predecessors)) != len(task.predecessors):
                raise ValueError('duplicate dependency')
            indegree[task.name] = len(task.predecessors)
            for p in task.predecessors:
                if p not in task_map: raise ValueError('missing predecessor')
                children[p].append(task.name)
            inputs.update(task.inputs); total += task.output_bound
        ready = deque(k for k,v in indegree.items() if v == 0); order = []
        while ready:
            node = ready.popleft(); order.append(node)
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0: ready.append(child)
        if len(order) != len(tasks): raise ValueError('cycle')
        with self.registry.lock:
            for h in inputs: self.registry.validate(h,scope)
            c = Contract(self.registry.epoch,scope,tuple(order),frozenset(inputs),total)
        self.tasks = task_map; self.contract = c
        return c

    def update_bounds(self, updates):
        old = self.contract
        if old is None: raise ValueError('no compiled plan')
        with self.registry.lock:
            if old.epoch != self.registry.epoch: raise ValueError('requires recompilation')
            delta = 0
            for name, bound in updates.items():
                if bound < 0 or name not in self.tasks: raise ValueError('invalid update')
                delta += bound-self.tasks[name].output_bound
            c = Contract(old.epoch,old.scope,old.order,old.pins,old.reservation_bytes+delta)
            for name,bound in updates.items():
                t=self.tasks[name]
                self.tasks[name]=Task(name,t.predecessors,t.inputs,bound)
            self.contract=c
            return c
