#!/usr/bin/env python3
"""Controlled registry-size scaling on real CXL; no LLM or concurrent-load claim."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil
import statistics
import tempfile
import time
import uuid

from dsec import DSec
from dataflow import compile_memory_contract
from run_profile import plan
from store import Store,dump


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repeats',type=int,default=5);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    jobs=[(n,policy,r) for n in [2,4,8,16,32,64] for policy in ['dataflow','refcount'] for r in range(a.repeats)]
    random.Random(71927).shuffle(jobs);rows=[]
    for n,policy,repeat in jobs:
        root=Path(tempfile.mkdtemp());payload=Path('/dev/shm')/('arvm-scale-'+uuid.uuid4().hex);store=None
        keys=['corpus','ledger','answer',*[f'{prefix}-{i}' for prefix in ['scratch','draft','review'] for i in range(n)]]
        tasks=plan(n,keys);row=dict(reviewers=n,tasks=len(tasks),objects=len(keys),policy=policy,repeat=repeat)
        try:
            store=Store.create(root/'store',payload,node=1,capacity=4096)
            for key in keys:store.seed(key,'controlled source')
            t=time.perf_counter_ns();compile_memory_contract(tasks,{key:0 for key in keys});row['compile_us']=(time.perf_counter_ns()-t)/1000
            t=time.perf_counter_ns();d=DSec.initialize(store,tasks,retention_policy=policy);row['initialize_us']=(time.perf_counter_ns()-t)/1000
            row['encoded_registry_bytes']=int.from_bytes(store.records['dsec-registry'][:8],'little')
            row['allocated_registry_bytes']=len(store.records['dsec-registry'])
            replacements={'draft-0':deepcopy(tasks['draft-0'])}
            t=time.perf_counter_ns();d.replan(replacements,expected_epoch=0);row['replan_us']=(time.perf_counter_ns()-t)/1000
            d.admit('draft-0',expected_epoch=1)
            samples=[]
            for i in range(53):
                t=time.perf_counter_ns()
                with d.pin('draft-0','corpus','read'):pass
                elapsed=(time.perf_counter_ns()-t)/1000
                if i>=3:samples.append(elapsed)
            row['pin_release_us']=samples;row['pin_release_median_us']=statistics.median(samples)
            row['status']='ok'
        except Exception as exc:
            row.update(status='rejected',error=repr(exc))
            if isinstance(exc,ValueError) and 'capacity exceeded' in str(exc):row['reason']='fixed 256 KiB registry capacity'
            else:raise
        finally:
            if store:store.close()
            shutil.rmtree(root);shutil.rmtree(payload,ignore_errors=True)
        rows.append(row)
        with (a.output/'trials.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
    summary=[]
    for n in [2,4,8,16,32,64]:
        for policy in ['dataflow','refcount']:
            rs=[r for r in rows if r['reviewers']==n and r['policy']==policy];good=[r for r in rs if r['status']=='ok']
            row=dict(reviewers=n,policy=policy,attempts=len(rs),accepted=len(good),rejected=len(rs)-len(good))
            if good:
                for key in ['compile_us','replan_us','encoded_registry_bytes','pin_release_median_us']:
                    row['median_'+key]=statistics.median(r[key] for r in good)
            summary.append(row)
    dump(a.output/'summary.json',dict(conditions=summary,scope='single controller, actual CXL registry, fixed 4 KiB objects; not concurrent throughput',
        timing='Python API with JSON serialization and audit logging; 3 warmups then 50 pin/release samples per trial',
        failures='All capacity rejections retained; no resizing or successful-only scalability claim',
        source_sha256={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                       for name in ['bench_registry_scale.py','dsec.py','dataflow.py','store.py','run_profile.py']}))
    (a.output/'source').mkdir()
    for name in ['bench_registry_scale.py','dsec.py','dataflow.py','store.py','run_profile.py']:shutil.copy2(Path(__file__).with_name(name),a.output/'source'/name)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
