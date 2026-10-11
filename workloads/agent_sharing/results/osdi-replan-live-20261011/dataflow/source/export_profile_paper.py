#!/usr/bin/env python3
"""Export validated empirical figures and numeric macros into the manuscript."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('suite',type=Path)
    p.add_argument('--paper',type=Path,required=True)
    a=p.parse_args();suite=a.suite;paper=a.paper
    report=json.loads((suite/'summary.json').read_text())
    assert report['attempted']==12 and report['complete']==12 and not report['errors']
    assert report['probe'], 'Run concurrent PSS probe before exporting the paper claims'
    out=paper/'experiments/results/shared_state_profile';out.mkdir(parents=True,exist_ok=True)
    for name in ['summary.json','validation.json','model-provenance.json','manifest.json','episodes.jsonl','graph-provenance.json']:
        shutil.copy2(suite/name,out/name)
    for name in ['observed-dataflow','sharing-profile','retention-timeline']:
        for ext in ['pdf','png']:
            shutil.copy2(suite/(name+'.'+ext),paper/'figures'/('profile-'+name+'.'+ext))
    aggregates={(r['workers'],r['policy']):r for r in report['aggregate']}
    dataflow=[r for r in report['episodes'] if r['policy']=='dataflow']
    risk=sum(next(t['reads_after_expiry'] for t in r['idle_expiry_replay'] if t['ttl_s']==1) for r in dataflow)
    read_count=sum(r['nonempty_reads'] for r in dataflow)
    macros=dict(ProfileEpisodes=report['attempted'],ProfileSessions=report['task_sessions'],ProfileEvents=report['events'],
        ProfileProtocol=report['protocol_complete'],ProfileIdleOneReads=risk,ProfileIdleOneDenominator=read_count,
        ProfileIdleOnePercent=f'{100*risk/read_count:.1f}',
        ProfileCorpusKiB=f"{dataflow[0]['corpus']['logical_bytes']/1024:.1f}")
    for n,label in [(2,'Two'),(4,'Four')]:
        graph=aggregates[n,'dataflow'];scope=aggregates[n,'scope']
        macros.update({f'Profile{label}ScopeKiB':f"{scope['median_average_payload_kib']:.1f}",
            f'Profile{label}GraphKiB':f"{graph['median_average_payload_kib']:.1f}",
            f'Profile{label}Saving':f"{100*(1-graph['median_average_payload_kib']/scope['median_average_payload_kib']):.1f}",
            f'Profile{label}ReplaySaving':f"{100*graph['median_same_timeline_payload_reduction']:.1f}",
            f'Profile{label}SharedPercent':f"{100*graph['shared_read_ratio']:.1f}",
            f'Profile{label}SpanSeconds':f"{graph['median_corpus_span_s']:.1f}",
            f'Profile{label}ActiveMillis':f"{graph['median_corpus_active_ms']:.1f}"})
    (out/'metrics.tex').write_text('% Generated from validated profile summary; do not edit numeric claims by hand.\n'+
        ''.join('\\newcommand{\\'+key+'}{'+str(value)+'}\n' for key,value in macros.items()))
    (out/'provenance.json').write_text(json.dumps(dict(source_suite=str(suite.resolve()),
        summary_sha256=hashlib.sha256((suite/'summary.json').read_bytes()).hexdigest(),
        export_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        reproduce='python3 workloads/agent_sharing/report_profile.py SUITE --probe PROBE; python3 workloads/agent_sharing/export_profile_paper.py SUITE --paper PAPER'),indent=2)+'\n')
    print(json.dumps(macros,indent=2))


if __name__=='__main__':main()
