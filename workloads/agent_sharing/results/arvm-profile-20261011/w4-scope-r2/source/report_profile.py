#!/usr/bin/env python3
"""Build reproducible paper figures/tables from all CXL profiling attempts."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import subprocess

from profile_analysis import analyze_profile,lines
from store import dump


def aggregate(rows):
    result=[]
    for workers in [2,4]:
        for policy in ['scope','dataflow']:
            matches=[r for r in rows if r['workers']==workers and r['policy']==policy]
            reads=sum(r['nonempty_reads'] for r in matches)
            result.append(dict(workers=workers,policy=policy,attempts=len(matches),
                completed=sum(r['status']=='complete' for r in matches),
                protocol_completed=sum(r['protocol_complete'] for r in matches),
                events=sum(r['events'] for r in matches),nonempty_reads=reads,
                shared_reads=sum(r['shared_nonempty_reads'] for r in matches),
                shared_read_ratio=sum(r['shared_nonempty_reads'] for r in matches)/reads,
                cross_task_reads=sum(r['cross_task_reads'] for r in matches),
                **{'median_'+key:statistics.median(r[key] for r in matches) for key in
                   ['average_payload_kib','payload_mib_seconds','e2e_s','compile_us','same_timeline_payload_reduction']},
                median_corpus_span_s=statistics.median(r['corpus']['observed_span_s'] for r in matches),
                median_corpus_active_ms=statistics.median(r['corpus']['active_access_ms'] for r in matches)))
    return result


def make_graph(episode,out):
    events=[e for f in (episode/'store/events').glob('*.jsonl') for e in lines(f)
            if e['kind']=='opencode-live' and e['status']=='ok']
    tasks=json.loads((episode/'memory-contract.json').read_text())['tasks']
    included={'corpus','answer'}|{k for k in [e['object'] for e in events] if k.startswith(('draft-','review-'))}
    nodes={};edges=Counter()
    for e in events:
        task='task:'+e['agent']
        nodes[task]=dict(kind='task',label=e['agent'].replace('draft-','Draft ').replace('review-','Peer '))
        obj=e['object']
        if obj not in included:continue
        if e['op']=='read' and e['logical_bytes_read']>0:
            name=f"object:{obj}@{e['version_read']}"
            nodes[name]=dict(kind='object',label=f"{obj} v{e['version_read']}")
            edges[(name,task)]+=1
        if 'version_written' in e:
            name=f"object:{obj}@{e['version_written']}"
            nodes[name]=dict(kind='object',label=f"{obj} v{e['version_written']}")
            edges[(task,name)]+=1
    # Omit empty CAS reads, mutable audit ledger and private scratch from the
    # rendered view only; all are kept in the full arvm-dataflow.json artifact.
    dot=['digraph profile {','rankdir=LR; bgcolor="white"; nodesep=0.18; ranksep=0.32;',
         'graph [fontname="Helvetica", fontsize=11];',
         'node [fontname="Helvetica", fontsize=10, penwidth=0.8, margin="0.07,0.04"];',
         'edge [color="#66727A", arrowsize=0.55, penwidth=0.75];']
    ids={name:f'n{i}' for i,name in enumerate(nodes)}
    for name,props in nodes.items():
        shape,color,fill=('box','#A0522D','#FCF0E8') if props['kind']=='task' else ('ellipse','#287B8E','#EAF4F6')
        dot.append(f'{ids[name]} [label={json.dumps(props["label"])}, shape={shape}, color="{color}", style=filled, fillcolor="{fill}"];')
    for (source,dest),count in edges.items():
        extra=' [style=dashed, color="#97A2A8"]' if source.startswith('object:corpus') else ''
        dot.append(f'{ids[source]} -> {ids[dest]}{extra};')
    for phase in ['draft','peer-review','synthesis']:
        names=[ids['task:'+name] for name,t in tasks.items() if t['phase']==phase]
        dot.append('{rank=same; '+';'.join(names)+';}')
    dot.append('}')
    path=out/'observed-dataflow.dot';path.write_text('\n'.join(dot)+'\n')
    for ext in ['pdf','png']:
        subprocess.run(['dot','-T'+ext,'-Gdpi=180',str(path),'-o',str(out/('observed-dataflow.'+ext))],check=True)
    dump(out/'graph-provenance.json',dict(episode=episode.name,selection='median-duration 4-worker dataflow episode',
        rendered_nodes=nodes,rendered_edges=[dict(source=a,destination=b,reads=n) for (a,b),n in edges.items()],
        omissions=['empty CAS reads','ledger','scratch'],
        dashed_edges='observed common-corpus reads; solid edges are observed publication and consumption'))


def figures(out,rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'pdf.fonttype':42})
    colors={'scope':'#59666E','dataflow':'#287B8E'}
    fig,axes=plt.subplots(1,2,figsize=(7.0,2.8))
    for j,policy in enumerate(['scope','dataflow']):
        for i,n in enumerate([2,4]):
            values=[r['average_payload_kib'] for r in rows if r['workers']==n and r['policy']==policy]
            x=i+(j-.5)*.34
            axes[0].bar(x,statistics.median(values),width=.3,color=colors[policy],alpha=.8,
                        label={'scope':'Scope-only','dataflow':'Dataflow'}[policy] if i==0 else None)
            for k,y in enumerate(values):axes[0].plot(x+(k-1)*.06,y,'o',ms=3.3,color='#222222',markerfacecolor='white')
    axes[0].set(xticks=[0,1],xticklabels=['2 reviewers','4 reviewers'],ylabel='Average live payload (KiB)',
                ylim=(0,1050),title='Measured CXL payload retention')
    axes[0].legend(frameon=False,fontsize=8)
    thresholds=[.1,.5,1.,2.,5.]
    for n,marker in [(2,'o'),(4,'s')]:
        rs=[r for r in rows if r['workers']==n and r['policy']=='dataflow']
        denominator=sum(r['nonempty_reads'] for r in rs)
        rates=[100*sum(next(t['reads_after_expiry'] for t in r['idle_expiry_replay'] if t['ttl_s']==threshold) for r in rs)/denominator for threshold in thresholds]
        axes[1].plot(range(len(thresholds)),rates,'-'+marker,color='#287B8E' if n==2 else '#59666E',
                     label=f'{n} reviewers',markerfacecolor='white' if n==4 else '#287B8E',ms=4)
    axes[1].set(xticks=range(len(thresholds)),xticklabels=[str(t) for t in thresholds],ylim=(0,100),
                xlabel='Idle expiry threshold (s)',ylabel='Nonempty reads after expiry (%)',title='Idle-only reclamation replay')
    axes[1].legend(frameon=False,fontsize=8)
    for ax in axes:ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    fig.tight_layout(pad=.8)
    for ext in ['pdf','png']:fig.savefig(out/('sharing-profile.'+ext),dpi=200)
    plt.close(fig)
    # A separate audit timeline uses one measured run and its same-time replay.
    chosen=[r for r in rows if r['workers']==4 and r['policy']=='dataflow']
    median=statistics.median(r['e2e_s'] for r in chosen)
    selected=min(chosen,key=lambda r:(abs(r['e2e_s']-median),r['episode']))
    memory=lines(out/selected['episode']/'memory.jsonl');start=memory[0]['ns']
    fig,ax=plt.subplots(figsize=(7,2.7))
    ax.step([(r['ns']-start)/1e9 for r in memory],[r['payload_bytes']/1024 for r in memory],where='post',
            color='#287B8E',label='Measured dataflow')
    last_task=next(r for r in memory if r['label']=='task-finish:synthesis')
    ax.step([0,(last_task['ns']-start)/1e9,(memory[-1]['ns']-start)/1e9],
            [memory[0]['payload_bytes']/1024,64,0],where='post',ls='--',color='#59666E',label='Scope-only, same-time replay')
    for r in memory:
        if r['label'].startswith('phase-start:'):
            x=(r['ns']-start)/1e9;ax.axvline(x,color='#BBBBBB',lw=.6)
    ax.set(xlabel='Time since payload allocation (s)',ylabel='CXL payload (KiB)',ylim=(0,1050),title=selected['episode'])
    ax.legend(frameon=False,fontsize=8);ax.spines[['top','right']].set_visible(False);fig.tight_layout()
    for ext in ['pdf','png']:fig.savefig(out/('retention-timeline.'+ext),dpi=200)
    plt.close(fig)
    make_graph(out/selected['episode'],out)
    return selected['episode']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('suite',type=Path)
    p.add_argument('--probe',type=Path)
    a=p.parse_args();root=a.suite
    manifest=json.loads((root/'manifest.json').read_text());attempts=lines(root/'episodes.jsonl')
    assert len(attempts)==manifest['planned']
    rows=[];failures=[]
    for attempt in attempts:
        try:
            row=analyze_profile(root/attempt['episode']);row['episode']=attempt['episode'];rows.append(row)
        except Exception as exc:failures.append(dict(episode=attempt['episode'],error=repr(exc)))
    if failures:
        dump(root/'validation-failures.json',failures)
        raise RuntimeError('Failed attempts need review; see validation-failures.json')
    aggregates=aggregate(rows)
    selected=figures(root,rows)
    probe=lines(a.probe/'runs.jsonl') if a.probe else []
    if probe:
        assert len(probe)==12 and all(r['status']=='ok' for r in probe)
        for r in probe:
            assert r['dram_pss_kib']==(64*r['workers'] if r['mode']=='private-dram' else 0)
            assert abs(r['cxl_pss_kib']-64)<=r['workers']
    summary=dict(aggregate=aggregates,episodes=rows,probe=probe,representative_episode=selected,
        complete=sum(r['status']=='complete' for r in rows),attempted=len(attempts),
        protocol_complete=sum(r['protocol_complete'] for r in rows),events=sum(r['events'] for r in rows),
        task_sessions=sum(r['task_sessions'] for r in rows),errors=sum(r['errors'] for r in rows),
        comparison='live scope versus dataflow retention; same-time counterfactual and idle replay explicitly separate',
        source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [Path(__file__),Path(__file__).with_name('profile_analysis.py')]})
    dump(root/'summary.json',summary)
    (root/'analysis-source').mkdir(exist_ok=True)
    import shutil
    for name in summary['source_sha256']:shutil.copy2(Path(__file__).with_name(name),root/'analysis-source'/name)
    dump(root/'validation.json',dict(assessment='Share with caveats',attempted=len(attempts),
        completed=summary['complete'],protocol_completed=summary['protocol_complete'],events=summary['events'],
        errors=summary['errors'],checks=rows[0]['checks'],limitations=rows[0]['limitations'],
        sentry='Not measured; unavailable baseline remains outside this dataflow ablation'))
    print(json.dumps({k:summary[k] for k in ['aggregate','complete','attempted','protocol_complete','events','task_sessions','errors']},indent=2))


if __name__=='__main__':main()
