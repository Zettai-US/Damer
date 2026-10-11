#!/usr/bin/env python3
"""Concurrent mapping probe on the same real corpus, separate from LLM timing.

The copy baseline retains CXL source plus per-worker DRAM replicas. PSS samples
are taken by the parent while every child holds all tested mappings alive.
"""
import argparse
import ctypes
import hashlib
import json
import mmap
import multiprocessing as mp
import os
from pathlib import Path
import random
import re
import shutil
import time
import uuid

from store import Store,bind,address,verify_placement,dump


def worker(root,mode,conn):
    store=Store(root,f'probe-{os.getpid()}');private=None
    try:
        grant=store.backend.acquire('corpus','read',10)
        try:
            source=store._map('corpus')
            source_hash=hashlib.sha256(source).hexdigest()
            source_nodes=verify_placement(source,1)
            copy_ns=0
            if mode=='private-dram':
                begin=time.monotonic_ns()
                private=mmap.mmap(-1,len(source),flags=mmap.MAP_PRIVATE|mmap.MAP_ANONYMOUS)
                bind(private,0);private[:]=source
                copy_ns=time.monotonic_ns()-begin
                assert hashlib.sha256(private).hexdigest()==source_hash
            mappings=[dict(address=address(source),size=len(source),tier='cxl',nodes=source_nodes)]
            if private is not None:mappings.append(dict(address=address(private),size=len(private),tier='dram',nodes=verify_placement(private,0)))
        finally:store.backend.release(grant['token'])
        conn.send(dict(pid=os.getpid(),mappings=mappings,sha256=source_hash,copy_ns=copy_ns))
        if conn.recv()!='release':raise RuntimeError('unexpected controller message')
    finally:
        if private is not None:private.close()
        store.close();conn.close()


def mapping_pss(pid,entry):
    start=entry['address'];end=start+entry['size'];active=False
    for line in Path(f'/proc/{pid}/smaps').read_text().splitlines():
        header=re.match(r'^([0-9a-f]+)-([0-9a-f]+) ',line)
        if header:active=int(header[1],16)==start and int(header[2],16)==end
        elif active and line.startswith('Pss:'):return int(line.split()[1])
    raise RuntimeError('exact mapping absent from smaps; refusing approximate RSS attribution')


def trial(out,corpus,n,mode):
    payload=Path('/dev/shm')/('arvm-probe-'+uuid.uuid4().hex)
    store=Store.create(out/'store',payload,node=1,capacity=65536);children=[];pipes=[]
    try:
        store.seed('corpus',corpus)
        ctx=mp.get_context('spawn')
        for _ in range(n):
            parent,child=ctx.Pipe();proc=ctx.Process(target=worker,args=(store.root,mode,child))
            proc.start();child.close();children.append(proc);pipes.append(parent)
        details=[]
        for pipe in pipes:
            if not pipe.poll(30):raise TimeoutError('mapping worker did not become ready')
            details.append(pipe.recv())
        assert len({d['sha256'] for d in details})==1
        cxl=dram=0
        for d in details:
            for mapping in d['mappings']:
                mapping['pss_kib']=mapping_pss(d['pid'],mapping)
                if mapping['tier']=='cxl':cxl+=mapping['pss_kib']
                else:dram+=mapping['pss_kib']
        for pipe in pipes:pipe.send('release')
        for proc in children:
            proc.join(10)
            if proc.exitcode!=0:raise RuntimeError('mapping worker failed')
        assert abs(cxl-64)<=n # Linux per-process KiB rounding.
        assert dram==(64*n if mode=='private-dram' else 0)
        return dict(workers=n,mode=mode,cxl_pss_kib=cxl,dram_pss_kib=dram,total_pss_kib=cxl+dram,
                    logical_bytes=len(corpus.encode()),payload_capacity=65536,details=details,status='ok')
    finally:
        for proc in children:
            if proc.is_alive():proc.terminate()
            proc.join(5)
        for pipe in pipes:pipe.close()
        store.close();shutil.rmtree(payload)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    corpus=a.corpus.read_text()
    cases=[(n,mode,r) for n in [2,4] for mode in ['shared-cxl','private-dram'] for r in range(3)]
    random.Random(4128).shuffle(cases)
    dump(out/'manifest.json',dict(kind='controlled concurrent mapping probe, not agent latency',planned=len(cases),
        corpus_sha256=hashlib.sha256(corpus.encode()).hexdigest(),source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline='both modes retain CXL source; private mode adds one DRAM copy per worker'))
    shutil.copy2(__file__,out/'probe-source.py')
    for n,mode,r in cases:
        try:row=dict(repeat=r,**trial(out/f'w{n}-{mode}-r{r}',corpus,n,mode))
        except Exception as exc:row=dict(workers=n,mode=mode,repeat=r,status='failed',error=repr(exc))
        with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        print(json.dumps({k:v for k,v in row.items() if k!='details'}),flush=True)
    return 0 if all(r['status']=='ok' for r in map(json.loads,(out/'runs.jsonl').read_text().splitlines())) else 2


if __name__=='__main__':raise SystemExit(main())
