#!/usr/bin/env python3
"""Run bounded native experiments on verified CXL/DRAM NUMA nodes.
Each policy block starts fresh processes. Only buffers owned by this experiment
are cache-flushed. No system cache, swap, device, or scheduler setting changes.
"""
from pathlib import Path
import subprocess, json, random, time, os, signal, hashlib
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'experiments/results'
BIN=Path('/tmp/arvm-cxl-bench')


def free_node0():
    for line in Path('/sys/devices/system/node/node0/meminfo').read_text().splitlines():
        if 'MemFree:' in line:return int(line.split()[-2])*1024
    raise RuntimeError('No node 0 memory information')


def invoke(args):
    p=subprocess.Popen([str(BIN),*map(str,args)],stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:out,err=p.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid,signal.SIGKILL);p.communicate();raise
    if p.returncode:raise RuntimeError(f'{args}: exit {p.returncode}: {err}')
    return [json.loads(x) for x in out.splitlines() if x]


def main():
    OUT.mkdir(exist_ok=True)
    subprocess.run(['gcc','-O3','-march=native','-Wall','-Wextra','-pthread',
                    str(ROOT/'experiments/cxl_bench.c'),'-o',str(BIN)],check=True)
    rng=random.Random(602710)
    configs=[(n,s,r) for n in [1,4,8] for s in [1,2,4] for r in [1,4,16]]
    configs += [(1,32,r) for r in [1,4,16]]
    paths={k:(OUT/f'{k}.jsonl').open('w') for k in ['fanout','phases']}
    started=time.time();count=0;skipped=[]
    for block in range(5):
        rng.shuffle(configs)
        for n,size,scans in configs:
            # Leave a 48 MiB node-local free-memory margin before each allocation.
            if free_node0() < (n*size+48)*2**20:
                skipped.append(dict(suite='fanout',block=block,n=n,size=size,scans=scans))
                continue
            policies=['copy','copy-reuse','share'];rng.shuffle(policies)
            for policy in policies:
                rows=invoke(['fanout',n,size,scans,5,policy,block])
                for row in rows:row.update(block=block,source_sha256=hashlib.sha256((ROOT/'experiments/cxl_bench.c').read_bytes()).hexdigest());paths['fanout'].write(json.dumps(row)+'\n')
                paths['fanout'].flush();count+=len(rows)
        print(f'fanout block {block+1}/5: {count} observations; {time.time()-started:.1f}s',flush=True)
    configs=[(s,g) for s in [8,32] for g in [0,500,2000,10000]]
    for block in range(4):
        rng.shuffle(configs)
        for size,gap in configs:
            if free_node0() < (size+48)*2**20:
                skipped.append(dict(suite='phases',block=block,size=size,gap=gap));continue
            policies=[('demand',100),('lookahead',100),('lookahead',50),('lookahead',0)]
            rng.shuffle(policies)
            for policy,accuracy in policies:
                rows=invoke(['phases',size,3,20,policy,accuracy,gap,block])
                for row in rows:row.update(block=block);paths['phases'].write(json.dumps(row)+'\n')
                paths['phases'].flush();count+=len(rows)
        print(f'phase block {block+1}/4: {count} observations; {time.time()-started:.1f}s',flush=True)
    for f in paths.values():f.close()
    manifest={'seed':602710,'elapsed_s':time.time()-started,'observations':count,
              'skipped_for_memory_margin':skipped,'compiler':subprocess.check_output(['gcc','--version'],text=True).splitlines()[0],
              'flags':'-O3 -march=native -Wall -Wextra -pthread',
              'workers_cpu_ids':[2,8,14,20,3,9,15,21],'parent_cpu_id':23,
              'source_node':1,'private_node':0,'warmups_per_invocation':2,
              'timestamp_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
              'source_sha256':hashlib.sha256((ROOT/'experiments/cxl_bench.c').read_bytes()).hexdigest()}
    (OUT/'native_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':main()
