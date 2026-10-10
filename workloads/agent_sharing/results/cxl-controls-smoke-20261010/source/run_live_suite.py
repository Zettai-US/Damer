#!/usr/bin/env python3
"""Repeat two prescribed real-model tasks, preserving every attempted episode."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys
import time
import urllib.request

from store import dump
from dataflow import analyze_run
from serve_model import provenance


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--opencode',default='/home/jason/.opencode/bin/opencode')
    p.add_argument('--repeats',type=int,default=3)
    a=p.parse_args()
    if not 1<=a.repeats<=10:p.error('repeats must be 1..10')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    with urllib.request.urlopen('http://127.0.0.1:18081/health',timeout=5) as r:
        if json.load(r).get('status')!='ok':raise RuntimeError('local model not healthy; run serve_model.py')
    dump(out/'model-provenance.json',provenance())
    cases=[(w,r) for w in ['runtime-review','incident'] for r in range(a.repeats)]
    random.Random(6619).shuffle(cases)
    manifest=dict(kind='live-opencode-arvm-dataflow',seed=6619,planned_episodes=len(cases),
                  roles_per_episode=3,order=cases,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    dump(out/'manifest.json',manifest)
    rows=[]
    for workload,repeat in cases:
        episode=out/f'{workload}-r{repeat}'
        begin=time.monotonic_ns()
        proc=subprocess.run([sys.executable,str(Path(__file__).parent/'run_live.py'),'--opencode',a.opencode,
            '--output',str(episode),'--workload',workload,'--timeout','240'],capture_output=True,text=True)
        (out/f'{episode.name}.log').write_text(proc.stdout+proc.stderr)
        row=dict(episode=episode.name,workload=workload,repeat=repeat,returncode=proc.returncode,
                 duration_ns=time.monotonic_ns()-begin)
        try:
            em=json.loads((episode/'manifest.json').read_text());row['status']=em['status']
            report=analyze_run(episode)
            row.update(metrics=report['metrics'],grant_path=report['grant_path'])
        except Exception as exc:row.update(status='failed',error=repr(exc))
        rows.append(row)
        with (out/'episodes.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
        print(json.dumps(row),flush=True)
    manifest.update(finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                    complete=sum(r['status']=='complete' for r in rows),attempted=len(rows))
    dump(out/'manifest.json',manifest)
    return 0 if manifest['complete']==manifest['planned_episodes'] else 2


if __name__=='__main__':raise SystemExit(main())
