#!/usr/bin/env python3
"""Randomized paired policy experiment with all attempted episodes retained."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys
import time

from store import dump
from serve_model import provenance
from profile_analysis import analyze_profile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repeats',type=int,default=3)
    a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if not 1<=a.repeats<=10:p.error('repeats: 1..10')
    dump(out/'model-provenance.json',provenance())
    rng=random.Random(71291);cases=[]
    for repeat in range(a.repeats):
        block=[(workers,policy,repeat) for workers in [2,4] for policy in ['scope','dataflow']]
        rng.shuffle(block);cases.extend(block)
    manifest=dict(schema='arvm-cxl-profile-suite-v1',seed=71291,planned=len(cases),order=cases,
        started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),model_parallel_slots=2,
        note='Paired worker-count/repeat blocks; policies share graph/prompts but generated text can differ. No latency-speedup claim.')
    dump(out/'manifest.json',manifest)
    rows=[]
    for workers,policy,repeat in cases:
        name=f'w{workers}-{policy}-r{repeat}';episode=out/name
        with (out/(name+'.log')).open('w') as log:
            proc=subprocess.run([sys.executable,str(Path(__file__).with_name('run_profile.py')),
                '--output',str(episode),'--workers',str(workers),'--retention',policy],stdout=log,stderr=subprocess.STDOUT)
        row=dict(episode=name,workers=workers,policy=policy,repeat=repeat,returncode=proc.returncode)
        try:
            report=analyze_profile(episode)
            row.update(status=report['status'],protocol_complete=report['protocol_complete'],
                events=report['events'],payload_mib_seconds=report['payload_mib_seconds'],
                average_payload_kib=report['average_payload_kib'],e2e_s=report['e2e_s'])
        except Exception as exc:row.update(status='failed',error=repr(exc))
        rows.append(row)
        with (out/'episodes.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        print(json.dumps(row),flush=True)
    manifest.update(attempted=len(rows),complete=sum(r['status']=='complete' for r in rows),
        finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    dump(out/'manifest.json',manifest)
    return 0 if manifest['complete']==manifest['planned'] else 2


if __name__=='__main__':raise SystemExit(main())
