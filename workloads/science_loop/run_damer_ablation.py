#!/usr/bin/env python3
"""Paired with/without-Damer loops with identical numerical and data policies."""
import os
for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):os.environ[name]='1'
import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import numpy as np
import run as physical
import run_memsim as simulator
from barrier_control import BarrierController
from dsec import DSec

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--domain',choices=['physical','memsim'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repetitions',type=int,default=5)
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--hosts',nargs=2,default=['bana','6787p'])
    parser.add_argument('--server',type=Path,default=HERE/'build/memsim/cxlmemsim_server')
    args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);(out/'source').mkdir()
    files=list(HERE.glob('*.py'))+[HERE.parent/'agent_sharing'/n for n in ['store.py','dsec.py','dataflow.py','cxl_lock.py','build_cxl_lock.py']]
    sources={}
    for p in files:
        shutil.copy2(p,out/'source'/p.name);sources[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
    manifest=dict(schema='damer-ablation-v1',domain=args.domain,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        sources=sources,repetitions=args.repetitions,host_load=Path('/proc/loadavg').read_text(),
        meminfo=Path('/proc/meminfo').read_text(),python=sys.version,numpy=np.__version__,
        comparison='DSec object-use compilation and shared control vs fixed-stage barriers with manual lifetime',
        shared='Identical data policy, numerical seed, payload versions, CXL publication locks and process-exit boundaries',
        methods='{} randomized complete seed blocks; no discarded warmups; one four-iteration loop per observation.'.format(args.repetitions),random_order_seed_base=3911)
    if args.domain=='physical':manifest['hardware']=physical.hardware(1)
    else:
        manifest['server_sha256']=hashlib.sha256(args.server.read_bytes()).hexdigest()
        manifest['server_commit']=simulator.command(['git','-C','/root/CXLMemSim','rev-parse','HEAD'])
        manifest['server_build_flags']='Release; microbenchmarks OFF; SlugAllocator OFF; RDMA OFF'
        manifest['model']='default_latency=100 ns; GFAM/DCD disabled; capacity=64 MiB'
        manifest['hosts']=args.hosts
    policies=['cxl-shared','cxl-cache','file-mmap'] if args.domain=='physical' else ['refetch','retain','statistics']
    configs=[dict(kernel=k,controller=c,policy=p) for k in (['wave'] if args.smoke else ['gray-scott','wave'])
             for p in (policies[:1] if args.smoke else policies) for c in ['damer','barrier']]
    manifest['planned_trials']=len(configs)*args.repetitions
    simulator.dump(out/'manifest.json',manifest)
    remote_dir='/tmp/arvm-science-ablation-'+uuid.uuid4().hex;deployed=[]
    try:
        if args.domain=='memsim':
            for host in args.hosts:
                simulator.deploy(host,remote_dir);deployed.append(host)
        for rep in range(args.repetitions):
            order=list(configs);random.Random(3911+rep).shuffle(order)
            for index,c in enumerate(order):
                cls=DSec if c['controller']=='damer' else BarrierController
                physical.DSec=cls;simulator.DSec=cls
                cfg=dict(c,rep=rep,seed=712+rep)
                if args.domain=='physical':
                    cores=sorted(os.sched_getaffinity(0))
                    cfg.update(mode=cfg.pop('policy'),n=64 if args.smoke else 256,frames=16,steps=64,
                        rounds=4,initial=4,batch=2,members=2,contention=False,budget_mib=1024,
                        cores=cores[:8]+[cores[12]],checks='full',retention='dataflow')
                else:cfg.update(n=32 if args.smoke else 64,layout='split',latency_ns=100)
                name='b{}-{:02d}-{}-{}-{}'.format(rep,index,c['kernel'],c['policy'],c['controller'])
                print(json.dumps(dict(event='start',trial=name)),flush=True)
                try:
                    if args.domain=='physical':result=physical.trial(out/name,cfg)
                    else:result=simulator.trial(out/name,cfg,args.server,remote_dir,args.hosts)
                    row=dict(status='complete',trial=name,discovery_s=result['discovery_s'])
                except BaseException:
                    row=dict(status='failed',trial=name,error=traceback.format_exc())
                    simulator.dump(out/(name+'-failure.json'),row)
                    with (out/'trials.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
                    raise
                with (out/'trials.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
                print(json.dumps(row),flush=True)
        manifest['completed_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        simulator.dump(out/'manifest.json',manifest)
    finally:
        for host in deployed:
            subprocess.run(simulator.SSH+[host,'rm -rf -- '+simulator.shlex.quote(remote_dir)],check=False,timeout=30)


if __name__=='__main__':main()
