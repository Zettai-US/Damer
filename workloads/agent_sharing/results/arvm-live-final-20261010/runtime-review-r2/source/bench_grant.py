#!/usr/bin/env python3
"""Measure our CXL grant path; report Sentry unavailable unless actually measured.

No emulation or OpenShell substitution for the NVIDIA Sentry hardware path.
Round trip here is a local Python API call, not a network request.
"""
import argparse
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid

from store import Store,dump
from dsec import DSec
from analyze import percentile


def worker(root,agent,steps,ready,results):
    store=Store(root,agent)
    dsec=DSec(store)
    try:
        # Warm both shared control and data lock mappings outside timing.
        with dsec.pin(agent,"state","read"):pass
        ready.wait(30)
        ready.wait(30)
        samples=[]
        for _ in range(steps):
            begin=time.monotonic_ns()
            with dsec.pin(agent,"state","read"):pass
            samples.append(time.monotonic_ns()-begin)
        results.put(dict(agent=agent,samples_ns=samples))
        ready.wait(30)
    except BaseException as exc:
        results.put(dict(agent=agent,error=repr(exc)))
        ready.abort()
        raise
    finally:store.close()


def experiment(out,workers,steps):
    payload=Path('/dev/shm')/('damer-grant-'+uuid.uuid4().hex)
    store=Store.create(out/'store',payload,node=1,capacity=4096)
    processes=[]
    try:
        store.seed('state','shared state')
        dsec=DSec.initialize(store,{f'agent-{i}':dict(grants={'read':['state']}) for i in range(workers)})
        for i in range(workers):dsec.admit(f'agent-{i}')
        ctx=mp.get_context('spawn')
        barrier=ctx.Barrier(workers+1)
        queue=ctx.Queue()
        processes=[ctx.Process(target=worker,args=(store.root,f'agent-{i}',steps,barrier,queue)) for i in range(workers)]
        for p in processes:p.start()
        barrier.wait(30)
        begin=time.monotonic_ns()
        barrier.wait(30)
        records=[queue.get(timeout=30) for _ in processes]
        barrier.wait(30)
        elapsed=time.monotonic_ns()-begin
        for p in processes:
            p.join(10)
            if p.exitcode!=0:raise RuntimeError('grant worker failed')
        if any('error' in r for r in records):raise RuntimeError(records)
        samples=[v for r in records for v in r['samples_ns']]
        issue=[];revoke=[]
        # Separately time grant issuance and revoke. Revocation must deny a new
        # pin. These timings are single-controller, following the parallel run.
        for _ in range(steps):
            begin=time.monotonic_ns();dsec.revoke('agent-0','state');revoke.append(time.monotonic_ns()-begin)
            try:
                with dsec.pin('agent-0','state','read'):pass
            except PermissionError:pass
            else:raise AssertionError('revoked grant allowed access')
            begin=time.monotonic_ns();dsec.grant('agent-0','state',['read']);issue.append(time.monotonic_ns()-begin)
        for i in range(workers):dsec.finish(f'agent-{i}',True)
        dsec.snapshot()
        return dict(status='ok',workers=workers,steps=steps,duration_ns=elapsed,
            operations=workers*steps,throughput_ops_s=workers*steps/(elapsed/1e9),
            acquire_release_p50_us=percentile(samples,.5)/1000,acquire_release_p95_us=percentile(samples,.95)/1000,
            issue_p50_us=percentile(issue,.5)/1000,revoke_p50_us=percentile(revoke,.5)/1000,
            samples=records,issue_samples_ns=issue,revoke_samples_ns=revoke)
    finally:
        for p in processes:
            if p.is_alive():p.terminate()
            p.join(5)
        store.close()
        shutil.rmtree(payload)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repeats',type=int,default=5)
    p.add_argument('--steps',type=int,default=100)
    a=p.parse_args()
    if not 1<=a.repeats<=20 or not 1<=a.steps<=2000:p.error('bounded run sizes required')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    pci=subprocess.check_output(['lspci','-nn'],text=True)
    (out/'pci.txt').write_text(pci)
    manifest=dict(schema='arvm-grant-benchmark-v1',kind='measured local software grant control path',
        clock='monotonic_ns',repeats=a.repeats,steps=a.steps,seed=9917,
        sentry=dict(status='not_measured',reason='No configured NVIDIA Sentry endpoint/SDK and no BlueField device in this host PCI inventory',
            hardware_inventory='pci.txt',speedup=None,
            official_source='https://www.nvidia.com/en-us/solutions/ai/agent-safety/'),
        caveats=['Local CXL JSON registry under native CXL mutex; no DPU trust-domain equivalence',
            'Measured API includes Python, lock, serialization and trace logging; excludes LLM and data payload access',
            'Throughput includes process barriers/queue; per-operation timing excludes them',
            'ACL revoke blocks new MCP requests, not arbitrary CPU pointers or existing OS mappings'],
        source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in Path(__file__).parent.glob('*.py')})
    dump(out/'manifest.json',manifest)
    (out/'source').mkdir()
    for name in manifest['source_sha256']:shutil.copy2(Path(__file__).parent/name,out/'source'/name)
    cases=[(w,r) for w in [1,2,4] for r in range(a.repeats)]
    random.Random(9917).shuffle(cases)
    rows=[]
    for w,r in cases:
        name=f'w{w}-r{r}'
        try:row=dict(run_id=name,repeat=r,**experiment(out/name,w,a.steps))
        except Exception as exc:row=dict(run_id=name,workers=w,repeat=r,status='failed',error=repr(exc))
        rows.append(row)
        with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        print(json.dumps({k:v for k,v in row.items() if k not in {'samples','issue_samples_ns','revoke_samples_ns'}}),flush=True)
    summary=[]
    for w in [1,2,4]:
        good=[r for r in rows if r['workers']==w and r['status']=='ok']
        if good:summary.append(dict(workers=w,successful_runs=len(good),attempted_runs=a.repeats,
            **{key:statistics.median(r[key] for r in good) for key in ['throughput_ops_s','acquire_release_p50_us','acquire_release_p95_us','issue_p50_us','revoke_p50_us']}))
    dump(out/'summary.json',dict(grant=summary,sentry=manifest['sentry'],caveats=manifest['caveats']))
    return 0 if all(r['status']=='ok' for r in rows) else 2


if __name__=='__main__':raise SystemExit(main())
