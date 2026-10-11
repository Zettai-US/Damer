#!/usr/bin/env python3
"""Summarize measured CXL microbenchmarks and export paper figures.
Static OSDI figures use grayscale, medians, and a consistent size. Replicate
variation is exported to CSV; selected charts show ranges of block medians.
No measurements are interpreted as end-to-end LLM/agent performance.
"""
from pathlib import Path
from collections import defaultdict
import csv,json,math,statistics
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'experiments/results';FIG=ROOT/'figures'
plt.rcParams.update({'font.family':'serif','font.serif':['DejaVu Serif'],'font.size':9,
 'axes.labelsize':9,'axes.titlesize':9,'legend.fontsize':7.5,'xtick.labelsize':8,'ytick.labelsize':8,
 'pdf.fonttype':42,'axes.spines.top':False,'axes.spines.right':False,'axes.linewidth':.7})


def read(name):return [json.loads(x) for x in (OUT/f'{name}.jsonl').read_text().splitlines() if x]
def write(name,rows):
    with (OUT/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def save(fig,ax,name):
    ax.grid(axis='y',color='.88',linewidth=.45);ax.set_axisbelow(True);fig.tight_layout(pad=.5)
    fig.savefig(FIG/f'{name}.pdf');fig.savefig(FIG/f'{name}.png',dpi=200);plt.close(fig)
def stats(values):return dict(median=float(np.median(values)),q25=float(np.quantile(values,.25)),q75=float(np.quantile(values,.75)),count=len(values))


def main():
    fanout=read('fanout');phase=read('phases');compiler=read('compiler')
    groups=defaultdict(list)
    for r in fanout:groups[(r['workers'],r['bytes'],r['scans'],r['policy'])].append(r)
    rows=[];index={}
    for key,data in sorted(groups.items()):
        n,size,scans,policy=key;v=stats([x['duration_ns']/1e6 for x in data]);blocks=defaultdict(list)
        for x in data:blocks[x['block']].append(x['duration_ns']/1e6)
        bm=[float(np.median(x)) for x in blocks.values()]
        pss=[x['payload_pss_kib'] for x in data if x['payload_pss_kib']>=0]
        r=dict(workers=n,bytes=size,scans=scans,policy=policy,median_ms=v['median'],q25_ms=v['q25'],q75_ms=v['q75'],observations=v['count'],blocks=len(blocks),block_median_min_ms=min(bm),block_median_max_ms=max(bm),payload_pss_kib=float(np.median(pss)))
        rows.append(r);index[key]=r
    write('fanout_summary.csv',rows)
    ratios=[]
    for n,size,scans,p in index:
        if p=='share' and (n,size,scans,'copy-reuse') in index:
            ratios.append(dict(workers=n,bytes=size,scans=scans,reuse_copy_to_share=index[(n,size,scans,'copy-reuse')]['median_ms']/index[(n,size,scans,'share')]['median_ms'],fresh_copy_to_share=index[(n,size,scans,'copy')]['median_ms']/index[(n,size,scans,'share')]['median_ms']))
    write('fanout_ratios.csv',ratios)
    fig,ax=plt.subplots(figsize=(3.34,2.45));x=np.arange(3);width=.24
    for i,(policy,label,color,hatch) in enumerate([('copy','Fresh copy','.8','//'),('copy-reuse','Reused copy','.55',''),('share','CXL share','.15','')]):
        selected=[index[(n,4*2**20,1,policy)] for n in [1,4,8]]
        vals=[r['median_ms'] for r in selected]
        err=np.array([[max(0,r['median_ms']-r['block_median_min_ms']) for r in selected],[max(0,r['block_median_max_ms']-r['median_ms']) for r in selected]])
        ax.bar(x+(i-1)*width,vals,width,color=color,edgecolor='black',linewidth=.5,hatch=hatch,label=label,yerr=err,capsize=2,error_kw={'linewidth':.7})
    ax.set_xticks(x,['1','4','8']);ax.set(xlabel='Concurrent worker processes',ylabel='Data-stage latency (ms)',title='4 MiB input, one scan (measured)');ax.legend(frameon=False,loc='upper left')
    save(fig,ax,'measured_fanout')

    groups=defaultdict(list)
    for r in phase:groups[(r['bytes'],r['gap_us'],r['policy'],r['hint_accuracy_pct'])].append(r)
    rows=[];pi={}
    for key,data in sorted(groups.items()):
        size,gap,policy,accuracy=key;v=stats([x['duration_ns']/1e6 for x in data]);r=dict(bytes=size,gap_us=gap,policy=policy,hint_accuracy_pct=accuracy,median_episode_ms=v['median'],q25_ms=v['q25'],q75_ms=v['q75'],observations=v['count'],bytes_copied=int(np.median([x['bytes_copied'] for x in data])),steps=20)
        rows.append(r);pi[key]=r
    write('phase_summary.csv',rows)
    fig,ax=plt.subplots(figsize=(3.34,2.45))
    for acc,style,marker in [(100,'-','o'),(50,'--','s'),(0,':','^')]:
        gaps=[0,500,2000,10000];ys=[pi[(32*2**20,g,'lookahead',acc)]['median_episode_ms']/pi[(32*2**20,g,'demand',100)]['median_episode_ms'] for g in gaps]
        ax.plot(range(4),ys,style,color='.15',marker=marker,markersize=4,label=f'{acc}% correct')
    ax.axhline(1,color='.6',linewidth=.8);ax.set_xticks(range(4),['0','0.5','2','10']);ax.set(xlabel='Planning interval setting (ms)',ylabel='Lookahead / demand episode time',title='32 MiB phases (measured)');ax.legend(frameon=False,ncol=1,loc='upper right')
    save(fig,ax,'measured_phases')

    groups=defaultdict(list)
    for r in compiler:groups[r['nodes']].append(r)
    rows=[]
    for n,data in sorted(groups.items()):
        row={'nodes':n,'observations':len(data)}
        for name in ['compile','bound_update','admit']:
            v=stats([r[name+'_ns']/1000 for r in data]);row.update({name+'_median_us':v['median'],name+'_q25_us':v['q25'],name+'_q75_us':v['q75']})
        rows.append(row)
    write('compiler_summary.csv',rows)
    fig,ax=plt.subplots(figsize=(3.34,2.45))
    for name,label,style,marker in [('compile','Full graph','-','o'),('bound_update','One bound','--','s'),('admit','Admission',':','^')]:
        ax.plot([r['nodes'] for r in rows],[r[name+'_median_us'] for r in rows],style,color='.15',marker=marker,markersize=4,label=label)
    ax.set_xscale('log',base=4);ax.set_yscale('log');ax.set_xticks([r['nodes'] for r in rows],[str(r['nodes']) for r in rows]);ax.set(xlabel='Tasks in accepted plan',ylabel='Median control cost (microseconds)',title='Python control path (measured)');ax.legend(frameon=False,loc='upper left')
    save(fig,ax,'measured_compiler')
    summary={'fanout_observations':len(fanout),'phase_episodes':len(phase),'compiler_trials':len(compiler),'fanout_conditions':len(ratios),'vs_reuse_geomean':math.exp(statistics.mean(math.log(r['reuse_copy_to_share']) for r in ratios)),'vs_reuse_min':min(r['reuse_copy_to_share'] for r in ratios),'vs_reuse_max':max(r['reuse_copy_to_share'] for r in ratios),'vs_fresh_geomean':math.exp(statistics.mean(math.log(r['fresh_copy_to_share']) for r in ratios)),'example_8_workers_4MiB':[index[(8,4*2**20,1,p)] for p in ['copy','copy-reuse','share']],'phase_32MiB': [r for r in pi.values() if r['bytes']==32*2**20],'compiler':rows,'all_native_checksums_pass':all(r['checksum_ok'] for r in fanout+phase)}
    (OUT/'measured_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))

if __name__=='__main__':main()
