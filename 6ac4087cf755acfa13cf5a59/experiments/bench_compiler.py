#!/usr/bin/env python3
from pathlib import Path
import gc,json,os,time,random
from runtime import Registry,Compiler,Task
ROOT=Path(__file__).resolve().parents[1]

def main():
    os.sched_setaffinity(0,{2});rng=random.Random(2710)
    output=(ROOT/'experiments/results/compiler.jsonl').open('w')
    for n in [16,64,256,1024,4096]:
        for trial in range(100):
            r=Registry(2**40);h=r.publish('passage',2**20,{'a'})
            tasks=[Task(f't{i}',(f't{(i-1)//4}',) if i else (), (h,),4096) for i in range(n)]
            c=Compiler(r)
            start=time.perf_counter_ns();contract=c.compile(tasks,'a');full=time.perf_counter_ns()-start
            updates={f't{n-1}':8192}
            start=time.perf_counter_ns();new=c.update_bounds(updates);update=time.perf_counter_ns()-start
            start=time.perf_counter_ns();lease=r.admit(new);admit=time.perf_counter_ns()-start
            r.finish(lease)
            output.write(json.dumps(dict(nodes=n,trial=trial,compile_ns=full,bound_update_ns=update,
                 admit_ns=admit,unique_inputs=len(new.pins),reserved_bytes=new.reservation_bytes,
                 output_delta_bytes=new.reservation_bytes-contract.reservation_bytes))+'\n')
    output.close()

if __name__=='__main__':main()
