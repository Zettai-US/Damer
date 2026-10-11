#!/usr/bin/env python3
"""Live OpenCode integration check for incrementally accepted task fragments.

The harness chooses the fragments; this does not claim natural plan discovery.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from dataflow import analyze_run,compile_memory_contract
from dsec import DSec
from run_live import invoke
from run_profile import SRC,MODEL,plan,prompts,configure,checkpoint,collect_dead,check_task
from store import Store,dump


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--policy',choices=['dataflow','refcount'],required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ.setdefault('DAMER_LOCAL_API_KEY','local-experiment')
    payload=Path('/dev/shm')/('arvm-live-replan-'+uuid.uuid4().hex)
    store=Store.create(out/'store',payload,node=1,capacity=65536)
    n=2;keys=['corpus','ledger','answer',*[f'{prefix}-{i}' for prefix in ['scratch','draft','review'] for i in range(n)]]
    corpus_paths=[SRC/'fixtures/arvm_registry.py',SRC/'store.py',SRC/'dsec.py',SRC/'dataflow.py']
    corpus='Actual prototype modules for source review.\n'+'\n'.join('\n===== '+f.name+' =====\n'+f.read_text() for f in corpus_paths)
    tasks=plan(n,keys);instructions=prompts(n)
    for key in keys:store.seed(key,corpus if key=='corpus' else '')
    (out/'corpus.txt').write_text(corpus);(out/'source').mkdir()
    sources={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in SRC.glob('*.py')}
    for name in sources:shutil.copy2(SRC/name,out/'source'/name)
    phases=[[f'draft-{i}' for i in range(n)],[f'review-{i}' for i in range(n)],['synthesis']]
    initially_retained=[key for key in keys if not key.startswith('scratch-')]
    initial={name:tasks[name] for name in phases[0]}
    dsec=DSec.initialize(store,initial,retention_policy=a.policy,session_roots=initially_retained)
    t=time.monotonic_ns();contract=compile_memory_contract(initial,{key:0 for key in keys});compile_ns=time.monotonic_ns()-t
    dump(out/'fragment-0.json',contract)
    manifest=dict(schema='arvm-live-replan-v1',kind='opencode-live-incremental-fragments',model=MODEL,workers=n,
        retention_policy=a.policy,status='running',task_results=[],task_sessions=5,compile_ns=compile_ns,
        source_sha256=sources,scope='harness-selected fragments; same-model integration check, not a statistical performance comparison',
        model_service_slots=2,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    begin=time.monotonic_ns()
    try:
        for name in tasks:configure(out/name,store,name)
        checkpoint(store,dsec,out,'allocated')
        for epoch,names in enumerate(phases):
            if epoch:
                replacements={name:tasks[name] for name in names}
                dsec.replan(replacements,expected_epoch=epoch-1,authorized_new={name:tasks[name]['grants'] for name in names})
                # Publish new references before dropping roots: no reachability gap.
                release=[f'draft-{i}' for i in range(n)] if epoch==1 else ['corpus','ledger',*[f'review-{i}' for i in range(n)]]
                for key in release:dsec.release_root(key)
                with dsec.registry() as state:dump(out/f'fragment-{epoch}.json',state)
            for name in names:dsec.admit(name,expected_epoch=epoch)
            checkpoint(store,dsec,out,'phase-start:'+names[0])
            with ThreadPoolExecutor(max_workers=n) as pool:
                pending={pool.submit(invoke,str(SRC/'build/opencode'),out/name,MODEL,instructions[name],240,store.root):name for name in names}
                for future in as_completed(pending):
                    name=pending[future];result=future.result();result['protocol']=check_task(store,name,tasks[name])
                    published=all(json.loads((store.root/'meta'/(key+'.json')).read_text())['version']>0 for key in tasks[name]['effects'].get('write',[]))
                    result['published']=published;success=result['process_ok'] and published
                    dsec.finish(name,success);manifest['task_results'].append(result)
                    checkpoint(store,dsec,out,'task-finish:'+name);collect_dead(store,dsec,out)
                    dump(out/'manifest.json',manifest)
                    print(json.dumps(dict(task=name,policy=a.policy,success=success,protocol=result['protocol'])),flush=True)
            if not all(r['process_ok'] and r['published'] for r in manifest['task_results']):raise RuntimeError('task did not publish')
        store.trace_kind='verification';dump(out/'answer.json',store.access('answer'))
        dsec.release_root('answer');collect_dead(store,dsec,out);checkpoint(store,dsec,out,'complete')
        dsec.snapshot();manifest.update(status='complete',protocol_complete=all(r['protocol']['complete'] for r in manifest['task_results']),
            duration_ns=time.monotonic_ns()-begin,accepted_epochs=3)
        dump(out/'memory-contract.json',compile_memory_contract(tasks,{key:0 for key in keys}))
        dump(out/'manifest.json',manifest)
        report=analyze_run(out)
        events=[e for f in (store.root/'events').glob('*.jsonl') for e in map(json.loads,f.read_text().splitlines()) if e['kind']=='opencode-live']
        life=[e for f in store.root.glob('dsec-*.jsonl') for e in map(json.loads,f.read_text().splitlines())]
        assert len([e for e in life if e['event']=='replan'])==2
        assert not json.loads((out/'memory.jsonl').read_text().splitlines()[-1])['payload_bytes']
        dump(out/'validation.json',dict(status='complete',tasks=5,events=len(events),protocol_complete=manifest['protocol_complete'],
            replan_commits=2,epochs=3,all_payload_reclaimed=True,limitations=report['limitations']))
    except Exception as exc:
        manifest.update(status='failed',error=repr(exc),duration_ns=time.monotonic_ns()-begin);dump(out/'manifest.json',manifest);raise
    finally:
        store.close();shutil.rmtree(payload,ignore_errors=True)


if __name__=='__main__':main()
