"""Without-Damer control: fixed-stage barriers and manual object lifetimes.

This is a strong baseline for this specific loop, not a generic task runtime.
It shares the same CXL lock backend, bulk budget, immutable publication and
worker-exit boundary. It omits object-use compilation, authority/effect checks,
epochs and per-access pins. Stage records live only at the coordinator; the
shared record holds the allocation ledger needed by concurrent producers.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'agent_sharing'))
from store import dump


def stage_key(name):
    pieces=name.split('-')
    role=pieces[0]
    if role not in {'sim','train','select'}:
        raise ValueError('fixed-stage baseline does not support this task')
    round_id=int(pieces[-1][1:]) if pieces[-1].startswith('r') else int(pieces[1])
    return round_id, {'sim':0,'train':1,'select':2}[role]


class BarrierController:
    def __init__(self,store):
        self.store=store
        self.tasks={};self.roots=set();self.retired=[];self.epoch=0

    @classmethod
    def initialize(cls,store,specs,retention_policy='dataflow',session_roots=()):
        if specs:
            raise ValueError('baseline expects the discovery harness to supply one round at a time')
        self=cls(store);self.roots=set(session_roots)
        # Same registry reservation and mutex in both arms; only required
        # allocation metadata crosses the CXL interface in this baseline.
        store.record('dsec-registry',initial=dict(pins={},pending_uses={},session_roots=[],
                     schema=1,controller='fixed-stage-barrier'),capacity=262144)
        return self

    @contextmanager
    def registry(self):
        grant=self.store.backend.acquire('dsec-control','write',10)
        try:
            state=self.store.read_record('dsec-registry')
            yield state
            self.store.write_record('dsec-registry',state)
        finally:
            self.store.backend.release(grant['token'])

    def replan(self,specs,expected_epoch,cancel=(),authorized_new=None):
        if cancel or any(t['state']!='completed' for t in self.tasks.values()):
            raise ValueError('fixed-stage baseline cannot replace in-flight work')
        rounds={stage_key(name)[0] for name in specs}
        if len(rounds)!=1:
            raise ValueError('one scientific round required')
        # Specifications are retained for the audit, not compiled or published.
        for name,value in specs.items():
            if name in self.tasks:
                raise ValueError('task name reused')
            self.tasks[name]=dict(deepcopy(value),state='ready',generation=0)
        self.epoch+=1
        return self.epoch

    def admit(self,name,expected_epoch=None):
        round_id,stage=stage_key(name)
        task=self.tasks[name]
        if task['state']!='ready':raise RuntimeError('task is not ready')
        if any(t['state']!='completed' for n,t in self.tasks.items()
               if stage_key(n)[0]==round_id and stage_key(n)[1]<stage):
            raise RuntimeError('fixed-stage barrier has not completed')
        task.update(state='running',started_ns=time.monotonic_ns())

    def finish(self,name,success):
        task=self.tasks[name]
        if task['state']!='running':raise RuntimeError('task is not running')
        task.update(state='completed' if success else 'failed',ended_ns=time.monotonic_ns())

    @contextmanager
    def pin(self,agent,key,operation):
        # The harness's stage barriers and process joins ensure that consumers
        # drain before reuse. This baseline adds no per-object admission layer.
        yield

    def retire(self,key):
        if key in self.roots:raise RuntimeError('manual corpus lifetime is still active')
        if key.startswith('model-'):
            round_id=int(key.split('-')[1])
            selectors=[t for n,t in self.tasks.items() if stage_key(n)==(round_id,2)]
            if len(selectors)!=1 or selectors[0]['state']!='completed':
                raise RuntimeError('selection barrier has not drained')
        elif key.startswith('traj-'):
            if any(t['state']!='completed' for t in self.tasks.values()):
                raise RuntimeError('discovery loop has not drained')
        else:raise ValueError('unsupported baseline object')
        if key in self.retired:raise ValueError('double retirement')
        self.retired.append(key)

    def release_root(self,key):
        self.roots.remove(key)

    def snapshot(self):
        with self.registry() as shared:
            state=dict(shared,sandboxes=self.tasks,session_roots=sorted(self.roots),
                       retired=self.retired,plan_epoch=self.epoch,
                       retention_policy='manual-fixed-stage',
                       control_location='coordinator private state; CXL allocation ledger only')
        dump(self.store.root/'dsec.json',state)
