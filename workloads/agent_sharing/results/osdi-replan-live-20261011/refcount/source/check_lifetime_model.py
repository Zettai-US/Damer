#!/usr/bin/env python3
"""Bounded explicit-state check of the managed-object lifetime protocol.

Two tasks, two objects, one root, one pending-task replan. This is not a proof
of the implementation, OS mappings, crash recovery, or the CXL memory model.
"""
import argparse
from collections import deque
from dataclasses import dataclass,replace,asdict
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class State:
    a:int=0       # ready, running, exited, complete, cancelled
    b:int=0
    b_object:int=0
    pins:int=0
    mappings:int=0  # one cached mapping per task
    root:bool=True
    retired:int=0
    live:int=3
    epoch:int=0


def uses(state):
    return (1 if state.a in [0,1,2] else 0)|(1<<state.b_object if state.b in [0,1,2] else 0)


def required(state):
    active=(1 if state.pins&1 or state.mappings&1 else 0)|(1<<state.b_object if state.pins&2 or state.mappings&2 else 0)
    return uses(state)|active|(1 if state.root else 0)


def successors(s,mutant):
    for t in ['a','b']:
        phase=getattr(s,t);bit=1 if t=='a' else 2;obj=1 if t=='a' else 1<<s.b_object
        if phase==0 and s.live&obj and not s.retired&obj:
            yield 'admit '+t,replace(s,**{t:1})
        if phase==1 and not s.pins&bit and s.live&obj and not s.retired&obj:
            yield 'pin '+t,replace(s,pins=s.pins|bit,mappings=s.mappings|bit)
        if s.pins&bit:yield 'unpin '+t,replace(s,pins=s.pins&~bit)
        if phase==1 and not s.pins&bit:
            yield 'namespace exit '+t,replace(s,**{t:2},mappings=s.mappings&~bit)
        if phase==2 or mutant=='omit_namespace_drain' and phase==1 and not s.pins&bit:
            yield 'finish '+t,replace(s,**{t:3})
    if s.root:yield 'release root',replace(s,root=False)
    if s.b==0 and s.epoch==0:
        if not s.retired&2 and s.live&2 or mutant=='resurrect_after_retirement':
            yield 'replan b to y',replace(s,b_object=1,epoch=1)
        yield 'cancel pending b',replace(s,b=4,epoch=1)
    for i in range(2):
        bit=1<<i
        active=(1 if s.pins&1 else 0)|(1<<s.b_object if s.pins&2 else 0)|(1 if s.root else 0)
        retained=active|(uses(s) if mutant!='omit_future_uses' else 0)
        if s.live&bit and not s.retired&bit and not retained&bit:
            yield 'retire '+str(i),replace(s,retired=s.retired|bit)
        if s.live&bit and s.retired&bit:
            yield 'reclaim '+str(i),replace(s,live=s.live&~bit)


def explore(mutant=None):
    initial=State();queue=deque([initial]);parents={initial:None};edges=0
    while queue:
        s=queue.popleft()
        if required(s)&~s.live:
            trace=[];node=s
            while parents[node] is not None:
                previous,action=parents[node];trace.append(action);node=previous
            return dict(mode=mutant,holds=False,states=len(parents),transitions=edges,
                        shortest_counterexample=list(reversed(trace)),violating_state=asdict(s))
        for action,n in successors(s,mutant):
            edges+=1
            if n not in parents:parents[n]=(s,action);queue.append(n)
    return dict(mode='guarded protocol',holds=True,states=len(parents),transitions=edges,
                terminal_states=sum(not list(successors(s,mutant)) for s in parents))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    results=[explore()]+[explore(m) for m in ['omit_future_uses','omit_namespace_drain','resurrect_after_retirement']]
    assert results[0]['holds'] and all(not r['holds'] for r in results[1:])
    report=dict(bound='2 tasks, 2 objects, 1 session root, at most 1 pending-task replan/cancellation',
                invariant='accepted future uses, active pins, cached mappings and roots retain live backing',
                assumptions=['atomic registry transitions','trustworthy task effects','namespace exit acknowledged',
                             'no host crash, hardware coherence failure, untracked native access, or object-name reuse'],
                interpretation='counterexamples are deliberately weakened abstract protocols, not observed runtime failures',
                results=results,source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(results,indent=2))


if __name__=='__main__':main()
