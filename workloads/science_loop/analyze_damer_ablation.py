#!/usr/bin/env python3
"""Audit paired runtime ablations without combining controller arms."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import numpy as np


def analyze(root):
    paths=sorted(root.glob('*/manifest.json'))
    if (root/'manifest.json').exists():paths=[root/'manifest.json']
    rows=[]; pairs=defaultdict(dict); science=defaultdict(list); hashes={}; rechecks=[]
    for path in paths:
        m=json.loads(path.read_text()); domain=m['domain']; folder=path.parent
        assert m['schema']=='damer-ablation-v1' and 'completed_utc' in m
        for name,digest in m['sources'].items():
            assert hashlib.sha256((folder/'source'/name).read_bytes()).hexdigest()==digest
        inventory=[json.loads(line) for line in (folder/'trials.jsonl').read_text().splitlines()]
        assert len(inventory)==m['planned_trials']
        assert len({x['trial'] for x in inventory})==len(inventory)
        for item in inventory:
            assert item['status']=='complete',item
            trial=folder/item['trial']; data=json.loads((trial/'result.json').read_text()); c=data['config']; its=data['iterations']
            hashes[str(trial.relative_to(root))]=hashlib.sha256((trial/'result.json').read_bytes()).hexdigest()
            assert len(its)==4
            final=json.loads((trial/'control/dsec.json').read_text())
            assert not final['pins'] and not final['pending_uses'] and not final['session_roots']
            assert len(final['sandboxes'])==22 and len(final['retired'])==18
            assert all(t['state']=='completed' for t in final['sandboxes'].values())
            assert abs(sum(x['iteration_s'] for x in its)-data['discovery_s'])<.1
            assert [x['dataset_cases'] for x in its]==[4,6,8,10]
            assert all(len(set(x['selected']))==2 and np.isfinite(x['validation_nrmse']) for x in its)
            policy=c.get('policy',c.get('mode'))
            row=dict(domain=domain,trial=str(trial.relative_to(root)),kernel=c['kernel'],n=c['n'],policy=policy,
                     controller=c['controller'],rep=c['rep'],seed=c['seed'],discovery_s=data['discovery_s'],
                     iteration_mean_s=data['discovery_s']/4,setup_s=data['setup_s'],cleanup_s=data['cleanup_s'],
                     copy_or_fetch_mib=0.,replica_peak_mib=0.,live_payload_peak_mib=0.,
                     coordinator_requests=0,coordinator_read_mib=0.,coordinator_write_mib=0.,
                     remote_stage_mean_s=0.,validation_nrmse=its[-1]['validation_nrmse'])
            for metric in ['commit_s','simulation_s','training_s','selection_s','reclaim_s']:
                row[metric]=sum(x[metric] for x in its)/4
            row['other_s']=row['iteration_mean_s']-sum(row[k] for k in ['commit_s','simulation_s','training_s','selection_s','reclaim_s'])
            assert row['other_s']>=-1e-6
            if domain=='physical':
                assert data['all_bulk_released'] and final['bulk']['live_bytes']==0 and not final['bulk']['objects']
                assert len(data['tasks'])==22
                trajectories=sorted(t['trajectory_sha256'] for t in data['tasks'] if t['kind']=='simulate')
                row['copy_or_fetch_mib']=sum(t['bytes']['local_copy'] for t in data['tasks'])/2**20
                row['replica_peak_mib']=max(x['replica_bytes'] for x in its)/2**20
                row['live_payload_peak_mib']=max(x['bulk_bytes'] for x in its)/2**20
                for x in its:assert x['bulk_bytes']==(x['dataset_cases']*16+2*10)*2*c['n']**2*4
                for task in data['tasks']:
                    for p in task['placements']:
                        if p.get('residency_rechecks'):rechecks.append(dict(trial=item['trial'],task=task['task'],object=p['key'],checks=p['residency_rechecks']))
                        if p['node'] is not None and p.get('checked',True):assert set(p['pages'])=={str(p['node'])}
                        if p.get('replica_pages') is not None:assert set(p['replica_pages'])=={'0'}
                payload_signature=[(x['bulk_bytes'],x['replica_bytes']) for x in its]
            else:
                assert data['all_pins_roots_and_remote_caches_drained'] and len(data['hosts'])==2
                simulated={x['key']:x['sha256'] for x in data['simulations']};trajectories=sorted(simulated.values())
                assert len(simulated)==10
                cache=defaultdict(set);fetch_bytes=0;private_peak=0;payload_signature=[]
                for r,x in enumerate(its):
                    assert len(x['workers'])==2
                    private=0;members=[]
                    for w in x['workers']:
                        expected=set('traj-'+str(j) for j in range(4+2*r))
                        if policy!='refetch':expected-=cache[w['host']]
                        assert {v['key'] for v in w['fetched']}==expected
                        cache[w['host']]|=expected
                        for obj in w['fetched']:
                            assert obj['sha256']==simulated[obj['key']]
                            assert obj['bytes']==16*2*c['n']**2*4
                            fetch_bytes+=obj['bytes']
                        per_case=400+10*2*c['n']**2*4 if policy=='statistics' else 16*2*c['n']**2*4
                        assert w['cache_bytes']==(4+2*r)*per_case
                        private+=w['cache_bytes'];members+=w['members']
                    assert {v['member'] for v in members}=={0,1}
                    for member in members:assert member['model_sha256']==x['model_sha256'][member['member']]
                    private_peak=max(private_peak,private)
                    mean=np.load(trial/('validation-r'+str(r)+'.npy'));scores=np.load(trial/('scores-r'+str(r)+'.npy'))
                    assert np.isfinite(mean).all() and np.isfinite(scores).all()
                    assert hashlib.sha256(mean).hexdigest()==x['prediction_sha256']
                    payload_signature.append((x['live_payload_bytes'],private,x['model_sha256']))
                assert fetch_bytes==(56 if policy=='refetch' else 20)*16*2*c['n']**2*4
                row['copy_or_fetch_mib']=fetch_bytes/2**20;row['replica_peak_mib']=private_peak/2**20
                row['live_payload_peak_mib']=max(x['live_payload_bytes'] for x in its)/2**20
                p=data['coordinator_protocol'];row['coordinator_requests']=p['requests']
                row['coordinator_read_mib']=p['read_bytes']/2**20;row['coordinator_write_mib']=p['write_bytes']/2**20
                row['remote_stage_mean_s']=sum(x['remote_stage_s'] for x in its)/4
            signature=([x['selected'] for x in its],[x['prediction_sha256'] for x in its],trajectories)
            science[(domain,c['kernel'],c['n'],c['seed'])].append(signature)
            key=(domain,c['kernel'],c['n'],policy,c['seed'])
            assert c['controller'] not in pairs[key]
            pairs[key][c['controller']]=(row,signature,payload_signature)
            rows.append(row)
    assert rows
    for key,values in science.items():assert all(v==values[0] for v in values),('scientific mismatch',key)
    contrasts=defaultdict(list)
    for key,value in pairs.items():
        assert set(value)=={'damer','barrier'},('unpaired',key)
        a,sa,pa=value['damer'];b,sb,pb=value['barrier']
        assert sa==sb and pa==pb
        for metric in ['copy_or_fetch_mib','replica_peak_mib','live_payload_peak_mib']:assert a[metric]==b[metric]
        contrasts[key[:-1]].append(dict(seed=key[-1],ratio=a['discovery_s']/b['discovery_s'],
            delta_iteration_s=a['iteration_mean_s']-b['iteration_mean_s'],
            damer_iteration_s=a['iteration_mean_s'],barrier_iteration_s=b['iteration_mean_s'],
            coordinator_request_delta=a['coordinator_requests']-b['coordinator_requests']))
    summaries=[];rng=np.random.default_rng(83110)
    for key,values in sorted(contrasts.items()):
        ratios=[v['ratio'] for v in values];boot=np.median(rng.choice(ratios,(10000,len(ratios)),replace=True),axis=1)
        summaries.append(dict(zip(['domain','kernel','n','policy'],key),pairs=len(values),
            ratio_median=statistics.median(ratios),bootstrap95=np.quantile(boot,[.025,.975]).tolist(),
            delta_iteration_s_median=statistics.median(v['delta_iteration_s'] for v in values),
            damer_iteration_s_median=statistics.median(v['damer_iteration_s'] for v in values),
            barrier_iteration_s_median=statistics.median(v['barrier_iteration_s'] for v in values),observations=values))
    out=root/'analysis';out.mkdir(exist_ok=True)
    with (out/'trials.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (out/'contrasts.json').write_text(json.dumps(summaries,indent=2)+'\n')
    validation=dict(status='pass',loops=len(rows),iterations=4*len(rows),paired_comparisons=len(pairs),
        scientific_equivalence_groups=len(science),result_hashes=hashes,residency_rechecks=rechecks,
        scope='Damer DSec integration versus hand-written fixed-stage lifecycle; not complete hardware or compiler ablation.',
        timing='Paired whole discovery loops; four rounds are correlated. Five-block bootstrap intervals are descriptive.',
        caveat='Simulator wall time includes Python, full-registry serialization and TCP; it is not CXL hardware latency.')
    (out/'validation.json').write_text(json.dumps(validation,indent=2)+'\n')
    print(json.dumps({k:validation[k] for k in ['status','loops','iterations','paired_comparisons','scientific_equivalence_groups']}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);analyze(p.parse_args().root.resolve())
