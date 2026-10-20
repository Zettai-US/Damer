#!/usr/bin/env python3
"""Audit every trial and aggregate paired complete-loop observations."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics as st
import numpy as np


def analyze(root):
    groups=defaultdict(list); equivalence=defaultdict(list); rows=[]; failures=[]
    hash_sources={}; manifests=[]; iteration_count=0; rechecks=[]; task_count=0
    for manifest_path in sorted(root.glob('*/manifest.json')):
        manifest=json.loads(manifest_path.read_text()); manifests.append(manifest)
        if 'completed_utc' not in manifest: raise ValueError(f'incomplete suite {manifest_path}')
        for name,sha in manifest['sources'].items():
            path=manifest_path.parent/'source'/name
            assert hashlib.sha256(path.read_bytes()).hexdigest()==sha, path
            hash_sources[str(path.relative_to(root))]=sha
        records=[json.loads(s) for s in (manifest_path.parent/'trials.jsonl').read_text().splitlines()]
        assert len(records)==manifest['trials']
        for record in records:
            if record['status']!='complete':failures.append(record); continue
            path=manifest_path.parent/record['trial']/'result.json'
            d=json.loads(path.read_text()); c=d['config']; it=d['iterations']
            iteration_count+=len(it); task_count+=len(d['tasks'])
            assert len(it)==c['rounds']
            assert len(d['tasks'])==c['initial']+(c['rounds']-1)*c['batch']+c['rounds']*(c['members']+1)
            assert d['all_bulk_released']
            final=json.loads((path.parent/'control/dsec.json').read_text())
            assert not final['pins'] and not final['pending_uses'] and not final['session_roots']
            assert final['bulk']['live_bytes']==0 and not final['bulk']['objects']
            assert all(v['state']=='completed' for v in final['sandboxes'].values())
            assert abs(sum(x['iteration_s'] for x in it)-d['discovery_s'])<.01
            for i,x in enumerate(it):
                trajectory_bytes=x['dataset_cases']*c['frames']*2*c['n']**2*4
                model_bytes=c['members']*10*2*c['n']**2*4
                assert x['bulk_bytes']==trajectory_bytes+model_bytes
                assert np.isfinite(x['validation_nrmse']) and x['validation_nrmse']>=0
                assert len(x['selected'])==len(set(x['selected']))==c['batch']
            for t in d['tasks']:
                for p in t['placements']:
                    if p.get('residency_rechecks'):rechecks.append(dict(trial=record['trial'],task=t['task'],object=p['key'],checks=p['residency_rechecks']))
                    if p['node'] is not None and p.get('checked',True):
                        assert set(p['pages'])=={str(p['node'])}
                    if p.get('replica_pages') is not None: assert set(p['replica_pages'])=={'0'}
            # Contention, transport and metadata representation may not change
            # the algorithm. Different ensemble size is a different algorithm.
            eq=(c['kernel'],c['n'],c['steps'],c['members'],c['seed'],c['rounds'])
            signature=dict(choices=[x['selected'] for x in it],predictions=[x['prediction_sha256'] for x in it],
                           trajectories=sorted(t['trajectory_sha256'] for t in d['tasks'] if t['kind']=='simulate'))
            equivalence[eq].append((str(path.relative_to(root)),signature))
            row=dict(suite=manifest['suite'],trial=record['trial'],**{k:c[k] for k in ['kernel','n','steps','members','contention','mode','rep','seed']},
                     retention=c.get('retention','dataflow'),checks=c.get('checks','full'),discovery_s=d['discovery_s'],iteration_mean_s=d['discovery_s']/c['rounds'],
                     setup_s=d['setup_s'],cleanup_s=d['cleanup_s'],validation_nrmse=it[-1]['validation_nrmse'],
                     managed_peak_mib=max((x['bulk_bytes']+x['replica_bytes']+x['control_bytes'])/2**20 for x in it),
                     bulk_peak_mib=max(x['bulk_bytes']/2**20 for x in it),replica_peak_mib=max(x['replica_bytes']/2**20 for x in it),
                     local_copy_mib=sum(t['bytes']['local_copy'] for t in d['tasks'])/2**20,
                     os_read_mib=sum(t['io']['read_bytes'] for t in d['tasks'])/2**20,
                     os_write_mib=sum(t['io']['write_bytes'] for t in d['tasks'])/2**20,
                     validation_worker_s=sum(t['timings']['checks_s'] for t in d['tasks']),
                     load_worker_s=sum(t['timings']['load_s'] for t in d['tasks']))
            row['major_faults']=sum(t.get('major_faults',0) for t in d['tasks'])
            for metric in ['commit_s','simulation_s','training_s','selection_s','reclaim_s']:
                row[metric]=sum(x[metric] for x in it)/c['rounds']
            row['other_s']=row['iteration_mean_s']-sum(row[k] for k in ['commit_s','simulation_s','training_s','selection_s','reclaim_s'])
            assert row['other_s']>=-1e-7
            rows.append(row)
            group=tuple(row[k] for k in ['suite','kernel','n','steps','members','contention','mode','retention','checks'])
            groups[group].append(row)
    eq_checks=[]
    for key,values in equivalence.items():
        assert all(v==values[0][1] for _,v in values), (key,[p for p,_ in values])
        eq_checks.append(dict(config=key,trials=len(values),all_identical=True))
    aggregates=[]
    for key,values in sorted(groups.items()):
        agg=dict(zip(['suite','kernel','n','steps','members','contention','mode','retention','checks'],key),blocks=len(values))
        for metric in ['iteration_mean_s','discovery_s','setup_s','validation_nrmse','managed_peak_mib','bulk_peak_mib','replica_peak_mib','local_copy_mib','os_read_mib','os_write_mib']:
            vals=[v[metric] for v in values]
            agg[metric+'_median']=st.median(vals); agg[metric+'_min']=min(vals); agg[metric+'_max']=max(vals)
        aggregates.append(agg)
    # Pair seed blocks rather than treating the four correlated rounds as
    # independent replicates. Bootstrap intervals are descriptive at n=5.
    contrasts=[]
    sets=defaultdict(dict)
    for r in rows:
        key=tuple(r[k] for k in ['suite','kernel','n','steps','members','contention','retention','checks'])
        sets[key][(r['mode'],r['rep'])]=r
    rng=np.random.default_rng(83011)
    for key,values in sorted(sets.items()):
        modes={mode for mode,rep in values}
        if 'cxl-shared' not in modes:continue
        for mode in sorted(modes-{'cxl-shared'}):
            reps=sorted(rep for m,rep in values if m==mode and ('cxl-shared',rep) in values)
            if not reps:continue
            ratios=[values[mode,rep]['discovery_s']/values['cxl-shared',rep]['discovery_s'] for rep in reps]
            samples=np.median(rng.choice(ratios,(10000,len(ratios)),replace=True),axis=1)
            contrasts.append(dict(zip(['suite','kernel','n','steps','members','contention','retention','checks'],key),
                                  baseline=mode,paired_ratios=ratios,median=float(np.median(ratios)),
                                  bootstrap95=[float(v) for v in np.quantile(samples,[.025,.975])]))
    output=root/'analysis'; output.mkdir(exist_ok=True)
    for name,values in [('trials.csv',rows),('summary.csv',aggregates)]:
        with (output/name).open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(values[0]));w.writeheader();w.writerows(values)
    (output/'contrasts.json').write_text(json.dumps(contrasts,indent=2)+'\n')
    audit=dict(status='pass' if not failures else 'failed',loops=len(rows),iterations=iteration_count,tasks=task_count,
               suites=[dict(suite=m['suite'],trials=m['trials'],failed=m['failed']) for m in manifests],
               failures=failures,equivalence_checks=eq_checks,source_hashes=hash_sources,residency_rechecks=rechecks,
               checks=['complete trial inventory','source hashes','identical selected candidates, field hashes and predictions across policies',
                       'verified physical CXL and DRAM residency','managed backing reconciled to array shapes',
                       'all mappings drained by process exit before finish','all bulk objects released; zero pins/roots/future uses',
                       'phase wall times reconcile to iteration totals'],
               caveats=['Single host, trusted workers; no cross-host fabric sharing.',
                        'CPU polynomial field surrogate; GPU path not measured (system torch import failed).',
                        'Managed file and replica capacities are not whole-process RSS or host memory usage.',
                        'File mmap uses warm page cache; durable-file baseline has stronger persistence semantics.',
                        'Five randomized blocks on a shared host; intervals are conditional, not production guarantees.',
                        'Training data remains rooted throughout the loop; no dataflow-specific reclamation advantage claimed.',
                        'Application pool budget is fail-closed admission, not an elastic multi-host pool manager.'])
    (output/'validation.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps({k:audit[k] for k in ['status','loops','iterations','suites']}))
    return audit


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);a=p.parse_args();analyze(a.root)
