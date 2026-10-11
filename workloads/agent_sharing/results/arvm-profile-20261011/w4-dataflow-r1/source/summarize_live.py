#!/usr/bin/env python3
"""Audit all attempted episodes and draw measured arVM motivation figures."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import shutil

from dataflow import analyze_run
from store import dump
from analyze import percentile


def validate_grant(root):
    saved=json.loads((root/'summary.json').read_text())
    manifest=json.loads((root/'manifest.json').read_text())
    runs=[json.loads(line) for line in (root/'runs.jsonl').read_text().splitlines()]
    assert len(runs)==3*manifest['repeats']
    for name,digest in manifest['source_sha256'].items():
        assert hashlib.sha256((root/'source'/name).read_bytes()).hexdigest()==digest
    for row in runs:
        assert row['status']=='ok'
        samples=[x for agent in row['samples'] for x in agent['samples_ns']]
        assert len(samples)==row['operations']==row['steps']*row['workers']
        assert percentile(samples,.5)/1000==row['acquire_release_p50_us']
        assert percentile(samples,.95)/1000==row['acquire_release_p95_us']
        assert len(row['issue_samples_ns'])==len(row['revoke_samples_ns'])==row['steps']
        assert percentile(row['issue_samples_ns'],.5)/1000==row['issue_p50_us']
        assert percentile(row['revoke_samples_ns'],.5)/1000==row['revoke_p50_us']
        assert row['throughput_ops_s']==len(samples)/(row['duration_ns']/1e9)
        state=json.loads((root/row['run_id']/'store/dsec.json').read_text())
        assert not state['pins'] and all(t['state']=='completed' for t in state['sandboxes'].values())
    for aggregate in saved['grant']:
        matching=[r for r in runs if r['workers']==aggregate['workers']]
        assert len(matching)==aggregate['successful_runs']==aggregate['attempted_runs']
        for metric in ['throughput_ops_s','acquire_release_p50_us','acquire_release_p95_us','issue_p50_us','revoke_p50_us']:
            assert statistics.median(r[metric] for r in matching)==aggregate[metric]
    assert saved['sentry']['status']=='not_measured' and saved['sentry']['speedup'] is None
    dump(root/'validation.json',dict(assessment='Share with caveats',trials=len(runs),
        access_pairs=sum(r['operations'] for r in runs),issue_revoke_pairs=sum(r['steps'] for r in runs),
        checks=['source snapshots','all raw sample counts and quantiles','throughput denominator',
                'trial aggregation','finished tasks and drained pins'],sentry='not_measured'))
    return saved


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('suite',type=Path)
    p.add_argument('--grant',type=Path,required=True)
    a=p.parse_args();root=a.suite
    attempts=[json.loads(line) for line in (root/'episodes.jsonl').read_text().splitlines()]
    manifest=json.loads((root/'manifest.json').read_text())
    if len(attempts)!=manifest['planned_episodes']:raise ValueError('missing attempted episodes')
    rows=[]
    for attempt in attempts:
        episode=root/attempt['episode']
        m=json.loads((episode/'manifest.json').read_text())
        for name,digest in m['source_sha256'].items():
            if hashlib.sha256((episode/'source'/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('source provenance mismatch')
        r=analyze_run(episode)
        events=[json.loads(s) for f in (episode/'store/events').glob('*.jsonl') for s in f.read_text().splitlines()]
        good=[e for e in events if e['kind']=='opencode-live' and e['status']=='ok']
        reads=[e for e in good if e['op']=='read']
        required={(f'analyst-{i}',k) for i in range(2) for k in ['evidence','ledger',f'finding-{i}']}
        required|={('synthesizer',k) for k in ['evidence','ledger','finding-0','finding-1','synthesis']}
        missing=sorted(required-{(e['agent'],e['object']) for e in reads})
        for i in range(2):
            app=[e for e in good if e['agent']==f'analyst-{i}' and e['object']=='ledger' and e['op']=='append']
            later=[e for e in reads if e['agent']==f'analyst-{i}' and e['object']=='ledger' and app and e['request_ns']>app[0]['end_ns']]
            if not app or not later:missing.append((f'analyst-{i}','ledger append then reread'))
        nonempty=[e for e in reads if e['logical_bytes_read']>0]
        shared={(v['object'],v['version']) for v in r['versions'] if v['reader_fanout']>=2}
        nonempty_shared=sum((e['object'],e['version_read']) in shared for e in nonempty)
        evidence=next(v for v in r['versions'] if v['object']=='evidence' and v['version']==0)
        row=dict(episode=attempt['episode'],workload=attempt['workload'],status=attempt['status'],
            required_tool_protocol_complete=not missing,missing_required_accesses=missing,
            duration_s=attempt['duration_ns']/1e9,**r['metrics'],
            nonempty_reads=len(nonempty),nonempty_shared_reads=nonempty_shared,
            nonempty_shared_read_ratio=nonempty_shared/len(nonempty) if nonempty else None,
            evidence_reader_fanout=evidence['reader_fanout'],
            evidence_span_ms=evidence['observed_span_ns']/1e6,
            evidence_active_ms=evidence['active_union_ns']/1e6,
            evidence_idle_ms=evidence['observed_idle_gap_ns']/1e6)
        rows.append(row)
    groups=[]
    for workload in sorted({r['workload'] for r in rows}):
        rs=[r for r in rows if r['workload']==workload]
        nr=sum(r['nonempty_reads'] for r in rs);sr=sum(r['nonempty_shared_reads'] for r in rs)
        groups.append(dict(workload=workload,attempted=len(rs),pipeline_complete=sum(r['status']=='complete' for r in rs),
            required_tool_protocol_complete=sum(r['required_tool_protocol_complete'] for r in rs),
            events=sum(r['events'] for r in rs),explicit_reads=sum(r['readonly_operations'] for r in rs),
            nonempty_shared_reads=sr,nonempty_reads=nr,nonempty_shared_read_ratio=sr/nr if nr else None,
            cross_agent_reads=sum(r['cross_agent_read_operations'] for r in rs),
            contended=sum(r['lock_contended'] for r in rs),
            median_e2e_s=statistics.median(r['duration_s'] for r in rs),
            median_evidence_span_ms=statistics.median(r['evidence_span_ms'] for r in rs),
            median_evidence_active_ms=statistics.median(r['evidence_active_ms'] for r in rs)))
    grant=validate_grant(a.grant)
    analysis_files=[Path(__file__),Path(__file__).with_name('dataflow.py'),Path(__file__).with_name('analyze.py')]
    snapshot=root/'analysis-source';snapshot.mkdir(exist_ok=True)
    for f in analysis_files:shutil.copy2(f,snapshot/f.name)
    report=dict(schema='arvm-motivation-report-v1',episodes=rows,aggregate=groups,performance=grant,
        analysis_source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in analysis_files},
        limitations=['Prescribed 3-agent tasks, 3 repeats each; no claim of natural agent workload prevalence',
          'Pipeline completion means publications; stricter required-tool compliance is independently audited and may differ',
          'No independent semantic answer-quality scorer; all attempts, including protocol omissions, retained',
          'CXL allocation is shared by trusted MCP tool processes; OpenCode/LLM receives copied bounded text',
          'Measured warm software grant API; NVIDIA Sentry is not measured and no relative speedup is reported',
          'CXL SpinLock is exclusive even for reads; single host, no cross-host or DPU security equivalence'])
    dump(root/'summary.json',report)
    plot(root,rows,grant)
    dump(root/'validation.json',dict(assessment='Share with caveats',attempted=len(rows),
        pipeline_complete=sum(r['status']=='complete' for r in rows),
        strict_tool_protocol_complete=sum(r['required_tool_protocol_complete'] for r in rows),
        recorded_events=sum(r['events'] for r in rows),errors=sum(r['errors'] for r in rows),
        checks=['source snapshots','version lineage','CXL payload/lock residency','balanced pins',
                'all planned attempts','independent required-tool coverage','nonempty read denominators'],
        incomplete=['NVIDIA Sentry comparison requires actual service/hardware access'],caveats=report['limitations']))
    print(json.dumps(groups,indent=2))


def plot(root,rows,grant):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'pdf.fonttype':42})
    fig,axes=plt.subplots(1,3,figsize=(13.2,4.2))
    colors={'runtime-review':'#287B8E','incident':'#BD5D37'}
    for w,color in colors.items():
        rs=[r for r in rows if r['workload']==w]
        x=0 if w=='runtime-review' else 1
        ys=[100*r['nonempty_shared_read_ratio'] for r in rs]
        axes[0].scatter([x-.09,x,x+.09][:len(ys)],ys,color=color,s=35,zorder=3)
        axes[0].plot([x-.22,x+.22],[statistics.median(ys)]*2,color=color,lw=3)
    axes[0].set(xticks=[0,1],xticklabels=['Runtime review','Incident review'],ylim=(0,100),
                ylabel='Same-version shared reads (%)',title='Sharing in observed tool reads')
    # Log scale shows the several orders of magnitude between critical-section
    # activity and the interval spanning actual consumers; all points displayed.
    for i,r in enumerate(rows):
        axes[1].scatter(i,r['evidence_span_ms'],color='#287B8E',marker='o',label='Observed span' if i==0 else None)
        axes[1].scatter(i,r['evidence_active_ms'],color='#BD5D37',marker='x',label='Active access' if i==0 else None)
    axes[1].set(yscale='log',xlabel='Episode (attempt order)',ylabel='Evidence version interval (ms)',
                title='State spans gaps between accesses',xticks=list(range(len(rows))),xticklabels=list(range(1,len(rows)+1)))
    axes[1].legend(frameon=False,loc='upper left',fontsize=9)
    gs=grant['grant'];xs=[r['workers'] for r in gs]
    axes[2].plot(xs,[r['acquire_release_p50_us'] for r in gs],'-o',label='p50')
    axes[2].plot(xs,[r['acquire_release_p95_us'] for r in gs],'-s',label='p95')
    axes[2].set(ylim=(0,None),xticks=xs,xlabel='Concurrent workers',ylabel='Grant check + pin/release (µs)',
                title='Local grant path; 5 trials / point')
    axes[2].legend(frameon=False)
    for ax in axes:ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.18)
    fig.text(.5,.04,'Left: episode dots and median bars; nonempty reads.  Right: median of trial quantiles.\nReal OpenCode + local Qwen; prescribed tasks.  NVIDIA Sentry: not measured.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.09,1,1])
    fig.savefig(root/'arvm-motivation.png',dpi=180)
    fig.savefig(root/'arvm-motivation.pdf')
    plt.close(fig)


if __name__=='__main__':main()
