#!/usr/bin/env python3
"""Audit all trials against stronger lifetime baselines and full allocation costs.

Counterfactual packing and cache replacement are offline models, not measurements.
"""
import argparse
from collections import Counter, OrderedDict, defaultdict
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics

from profile_analysis import lines
from store import dump


def integral(points,field):
    return sum(a[field]*(b['ns']-a['ns'])/1e9 for a,b in zip(points,points[1:]))


def uses(spec):
    return set(k for values in spec.get('effects',spec['grants']).values() for k in values)


def recoverable_lru(events,slots,capacity):
    """Serial object-slot cache replay with a separate lossless backing tier.

    Eviction writes dirty slots; full overwrite and known empty versions avoid
    backing reads. Final dirty slots are flushed. No access latency is modeled.
    """
    cache=OrderedDict();loads=writebacks=evictions=misses=0
    for event in events:
        key=event['object']
        if key not in cache:
            misses+=1
            if len(cache)==slots:
                _,dirty=cache.popitem(last=False)
                writebacks+=bool(dirty);evictions+=1
            if event['op']!='write' and event['logical_bytes_read']>0:loads+=1
            cache[key]=False
        cache.move_to_end(key)
        cache[key]|=event['op'] in {'write','append'}
    writebacks+=sum(cache.values())
    return dict(slots=slots,accesses=len(events),slot_misses=misses,backing_loads=loads,
                dirty_writebacks=writebacks,evictions=evictions,read_kib=loads*capacity/1024,
                write_kib=writebacks*capacity/1024,resident_limit_kib=slots*capacity/1024,
                semantics='offline serial access replay; separate lossless backing; final flush included; no latency estimate')


def audit_episode(root):
    manifest=json.loads((root/'manifest.json').read_text())
    plan=json.loads((root/'memory-contract.json').read_text())['tasks']
    memory=lines(root/'memory.jsonl');start,end=memory[0]['ns'],memory[-1]['ns']
    duration=(end-start)/1e9
    lifecycle=sorted((e for p in (root/'store').glob('dsec-*.jsonl') for e in lines(p)),key=lambda e:e['ns'])
    events=sorted((e for p in (root/'store/events').glob('*.jsonl') for e in lines(p)
                   if e['status']=='ok' and e['kind']=='opencode-live'),key=lambda e:e['acquired_ns'])
    finished={e['agent']:e['ns'] for e in lifecycle if e['event']=='finish'}
    roots={e['object']:e['ns'] for e in lifecycle if e['event']=='release_root'}
    keys=set(memory[0]['objects']);sizes={k:v['allocated_bytes'] for k,v in memory[0]['objects'].items()}
    effects={name:uses(spec) for name,spec in plan.items()}
    graph_death={key:max([start,roots.get(key,start)]+[finished[name] for name in plan if key in effects[name]]) for key in keys}
    # Independent future-reference-count computation: no graph traversal.
    counts=Counter(key for names in effects.values() for key in names)
    remaining_roots=set(memory[0]['session_roots']);rc_death={}
    for event in lifecycle:
        if event['event']=='finish':
            for key in effects[event['agent']]:counts[key]-=1
        elif event['event']=='release_root':remaining_roots.remove(event['object'])
        for key in keys:
            if not counts[key] and key not in remaining_roots:rc_death.setdefault(key,event['ns'])
    assert graph_death==rc_death, 'same-information reference counting must match effect lifetimes'
    scope_death={key:max([start,roots.get(key,start)]+[finished[name] for name,spec in plan.items()
                      if any(key in ids for ids in spec['grants'].values())]) for key in keys}
    # Non-measured right-sizing counterfactual: per-object peak logical bytes,
    # page rounded, preallocated at t=0. Bounds are known retrospectively.
    peak={key:0 for key in keys}
    for e in events:peak[e['object']]=max(peak[e['object']],e['logical_bytes_read'],e.get('logical_bytes_written',0))
    packed={key:max(4096,math.ceil(value/4096)*4096) for key,value in peak.items()}
    def area(death,weight):return sum(weight[k]*(death[k]-start)/1e9 for k in keys)
    # Measured control allocations include registry, metadata and mutex pages.
    payload_area=integral(memory,'payload_bytes');control_area=integral(memory,'control_bytes')
    # Integrate current logical content along publications and measured unlink
    # checkpoints; this is accounting, not a physical compact allocation result.
    logical={key:0 for key in keys};logical['corpus']=len((root/'corpus.txt').read_bytes())
    timeline=[]
    for e in events:
        if 'logical_bytes_written' in e:timeline.append((e['published_written_ns'],0,e['object'],e['logical_bytes_written']))
    for a,b in zip(memory,memory[1:]):
        for key in set(a['objects'])-set(b['objects']):timeline.append((b['ns'],1,key,0))
    now=start;logical_area=0
    for ns,_,key,value in sorted(timeline):
        assert start<=ns<=end
        logical_area+=sum(logical.values())*(ns-now)/1e9
        logical[key]=value;now=ns
    logical_area+=sum(logical.values())*(end-now)/1e9
    frame_checks=0
    for row in memory:
        live=set(row['session_roots'])
        live.update(key for name,status in row['tasks'].items() if status in {'ready','running'} for key in effects[name])
        independently_live=set(row['session_roots'])|{key for key in keys if sum(row['tasks'][name] in {'ready','running'} and key in effects[name] for name in plan)>0}
        assert live==independently_live
        assert live<=set(row['objects']), 'a declared future use lost its backing'
        frame_checks+=1
    lru=[recoverable_lru(events,max(1,math.ceil(len(keys)*fraction)),next(iter(sizes.values()))) for fraction in [.25,.5,.75,1.]]
    return dict(episode=root.name,workers=manifest['workers'],policy=manifest['retention_policy'],
        protocol_complete=manifest['protocol_complete'],events=len(events),frame_checks=frame_checks,
        observed_payload_average_kib=payload_area/duration/1024,
        observed_control_average_kib=control_area/duration/1024,
        observed_managed_average_kib=(payload_area+control_area)/duration/1024,
        observed_logical_average_kib=logical_area/duration/1024,
        physical_payload_to_logical_ratio=payload_area/logical_area,
        same_info_rc_equal=True,graph_death_ns=graph_death,rc_death_ns=rc_death,
        graph_replay_average_kib=area(graph_death,sizes)/duration/1024,
        scope_replay_average_kib=area(scope_death,sizes)/duration/1024,
        compact_graph_average_kib=area(graph_death,packed)/duration/1024,
        compact_scope_average_kib=area(scope_death,packed)/duration/1024,
        compact_replay_reduction=1-area(graph_death,packed)/area(scope_death,packed),
        peak_logical_bytes=peak,compact_slot_bytes=packed,lru_replay=lru,
        lossless_backing_capacity_kib=sum(sizes.values())/1024,
        source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                       for name in ['manifest.json','memory-contract.json','memory.jsonl']})


def make_figure(out,rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'pdf.fonttype':42})
    fig,axes=plt.subplots(1,2,figsize=(7,2.65))
    for i,n in enumerate([2,4]):
        for j,p in enumerate(['scope','dataflow']):
            rs=[r for r in rows if r['workers']==n and r['policy']==p];x=2.6*i+j
            payload=statistics.median(r['observed_payload_average_kib'] for r in rs)
            total=statistics.median(r['observed_managed_average_kib'] for r in rs)
            axes[0].bar(x,payload,color='#287B8E',label='Payload' if i==j==0 else None)
            axes[0].bar(x,total-payload,bottom=payload,color='#A7AFB5',hatch='//',label='Control' if i==j==0 else None)
            for row in rs:axes[0].plot(x,row['observed_managed_average_kib'],'o',ms=3,color='#222222',mfc='white')
    axes[0].set(xticks=[0,1,2.6,3.6],xticklabels=['2/S','2/D','4/S','4/D'],ylabel='Average managed CXL (KiB)',
                title='Measured payload and control',ylim=(0,1500),xlabel='Reviewers / Scope or Dataflow')
    axes[0].legend(frameon=False,fontsize=8)
    for i,n in enumerate([2,4]):
        rs=[r for r in rows if r['workers']==n and r['policy']=='dataflow']
        for j,(field,color) in enumerate([('scope','#59666E'),('graph','#287B8E')]):
            values=[r['compact_'+field+'_average_kib'] for r in rs];x=i+(j-.5)*.34
            axes[1].bar(x,statistics.median(values),width=.3,color=color,label=('Scope-only' if j==0 else 'Future uses') if i==0 else None)
            for k,value in enumerate(values):axes[1].plot(x+(k-1)*.05,value,'o',ms=3,color='#222222',mfc='white')
    axes[1].set(xticks=[0,1],xticklabels=['2 reviewers','4 reviewers'],ylabel='Payload only (KiB)',
                title='Page-rounded size replay',ylim=(0,115))
    axes[1].legend(frameon=False,fontsize=8)
    for ax in axes:ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True)
    fig.tight_layout(pad=.8)
    for ext in ['pdf','png']:fig.savefig(out/('accounting-audit.'+ext),dpi=200)
    plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('suite',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    attempts=lines(a.suite/'episodes.jsonl');rows=[audit_episode(a.suite/r['episode']) for r in attempts]
    aggregates=[]
    for n in [2,4]:
        scope=[r for r in rows if r['workers']==n and r['policy']=='scope']
        graph=[r for r in rows if r['workers']==n and r['policy']=='dataflow']
        med=lambda rs,k:statistics.median(r[k] for r in rs)
        aggregates.append(dict(workers=n,scope_total_kib=med(scope,'observed_managed_average_kib'),
            graph_total_kib=med(graph,'observed_managed_average_kib'),
            total_reduction=1-med(graph,'observed_managed_average_kib')/med(scope,'observed_managed_average_kib'),
            graph_control_kib=med(graph,'observed_control_average_kib'),
            graph_logical_kib=med(graph,'observed_logical_average_kib'),
            compact_replay_reduction=med(graph,'compact_replay_reduction'),
            compact_scope_kib=med(graph,'compact_scope_average_kib'),compact_graph_kib=med(graph,'compact_graph_average_kib'),
            logical_expansion=med(graph,'physical_payload_to_logical_ratio')))
    report=dict(schema='arvm-evidence-audit-v1',episodes=rows,aggregate=aggregates,
        attempts=len(rows),reference_equivalent=sum(r['same_info_rc_equal'] for r in rows),
        frame_checks=sum(r['frame_checks'] for r in rows),
        scope='all original attempts; no new LLM sessions; measured allocation accounting plus explicitly offline replays',
        findings=['Future-aware reference counts reproduce every declared-object death time; no DAG-specific capacity advantage',
                  'Fixed slots amplify the payload-only saving; count registry and lock pages as well',
                  'Recoverable cache eviction preserves data but introduces transfers; idle gaps alone are not evidence of unsafe LRU'],
        limitations=['Right-sizing uses retrospectively known peak object size; not an implemented allocator',
                     'Cache replay is serial and assumes a separate lossless backing tier; no measured I/O latency or throughput',
                     'Three live repetitions per cell support descriptive statistics, not a robust tail or population estimate'])
    dump(a.output/'summary.json',report)
    dump(a.output/'chart-contract.json',dict(question='Does the headline payload saving survive complete accounting and right-sizing?',
         family='stacked and grouped bars',renderer='matplotlib',measured_rows=12,counterfactual_rows=6,
         palette='blue and neutral grey; hatch for control metadata',output='accounting-audit.pdf/png',
         qa='inspect rendered PDF; separate measured totals from offline right-sizing; zero baselines'))
    shutil.copy2(__file__,a.output/'audit-source.py');make_figure(a.output,rows)
    print(json.dumps({k:report[k] for k in ['attempts','reference_equivalent','frame_checks','aggregate']},indent=2))


if __name__=='__main__':main()
