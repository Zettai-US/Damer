#!/usr/bin/env python3
"""Audit complete simulator loops, byte accounting and scientific equivalence."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import numpy as np


def analyze(root):
    manifests = sorted(root.glob('*/manifest.json'))
    if (root/'manifest.json').exists():
        manifests = [root/'manifest.json']
    rows = []; groups = defaultdict(list); exact = defaultdict(list); numerical = defaultdict(list)
    failures = []; source_hashes = {}; results = []
    for path in manifests:
        manifest = json.loads(path.read_text()); folder = path.parent
        assert 'completed_utc' in manifest, ('incomplete suite',str(path))
        for name, digest in manifest['sources'].items():
            assert hashlib.sha256((folder/'source'/name).read_bytes()).hexdigest() == digest
            if name in source_hashes:
                assert source_hashes[name] == digest, ('mixed harness revisions',name)
            source_hashes[name] = digest
        inventory = [json.loads(s) for s in (folder/'trials.jsonl').read_text().splitlines()]
        assert len(inventory) == manifest['planned_trials']
        assert len({r['trial'] for r in inventory}) == len(inventory)
        for item in inventory:
            if item['status'] != 'complete':
                failures.append(item); continue
            trial = folder/item['trial']; result = json.loads((trial/'result.json').read_text())
            cfg = result['config']; iterations = result['iterations']
            assert len(iterations) == 4
            assert result['all_pins_roots_and_remote_caches_drained']
            assert abs(sum(x['iteration_s'] for x in iterations)-result['discovery_s']) < .1
            final = json.loads((trial/'control/dsec.json').read_text())
            assert not final['pins'] and not final['pending_uses'] and not final['session_roots']
            assert len(final['sandboxes']) == 22
            assert all(v['state']=='completed' for v in final['sandboxes'].values())
            assert len(final['retired']) == 18
            simulated = {x['key']:x['sha256'] for x in result['simulations']}
            assert len(simulated) == 10
            fetched_bytes = 0; remote_read = 0; remote_write = 0; remote_ops = 0
            model_latency_ns = result['coordinator_protocol']['modeled_latency_ns_sum']
            copy_peak = 0; cache_per_host = defaultdict(set); scientific=[]
            host_count = 2 if cfg['layout']=='split' else 1
            assert len(result['hosts']) == host_count
            for r,it in enumerate(iterations):
                assert it['dataset_cases'] == 4+2*r
                assert len(it['selected']) == len(set(it['selected'])) == 2
                assert len(it['workers']) == host_count
                cache_sum = 0; members=[]
                for worker in it['workers']:
                    host = worker['host']; members += worker['members']
                    expected = set('traj-'+str(j) for j in range(4+2*r))
                    if cfg['policy'] != 'refetch':
                        expected -= cache_per_host[host]
                    fetched = {x['key'] for x in worker['fetched']}
                    assert fetched == expected
                    cache_per_host[host] |= fetched
                    for obj in worker['fetched']:
                        assert obj['sha256'] == simulated[obj['key']]
                        assert obj['bytes'] == 16*2*cfg['n']**2*4
                        fetched_bytes += obj['bytes']
                    per_case = (400+10*2*cfg['n']**2*4) if cfg['policy']=='statistics' else 16*2*cfg['n']**2*4
                    assert worker['cache_bytes'] == (4+2*r)*per_case
                    cache_sum += worker['cache_bytes']
                    protocols = [worker['protocol']]+[x['protocol'] for x in worker['members']]
                    for p in protocols:
                        remote_read += p['read_bytes']; remote_write += p['write_bytes']; remote_ops += p['requests']
                        model_latency_ns += p['modeled_latency_ns_sum']
                assert {m['member'] for m in members} == {0,1}
                for m in members:
                    assert m['model_sha256'] == it['model_sha256'][m['member']]
                copy_peak = max(copy_peak,cache_sum)
                mean = np.load(trial/('validation-r'+str(r)+'.npy'))
                score = np.load(trial/('scores-r'+str(r)+'.npy'))
                assert hashlib.sha256(mean).hexdigest() == it['prediction_sha256']
                assert np.isfinite(mean).all() and np.isfinite(score).all()
                scientific.append((mean,score,it['selected']))
            total_reads = (28 if cfg['policy']=='refetch' else 10)*host_count
            assert fetched_bytes == total_reads*16*2*cfg['n']**2*4
            key = (cfg['kernel'],cfg['n'],cfg['seed'],cfg['layout'])
            signature = ([it['selected'] for it in iterations], [it['prediction_sha256'] for it in iterations], simulated)
            exact[key].append((str(trial),signature))
            numerical[(cfg['kernel'],cfg['n'],cfg['seed'])].append((str(trial),scientific))
            row = dict(suite=manifest['suite'],trial=str(trial.relative_to(root)),**cfg,
                       discovery_s=result['discovery_s'],iteration_mean_s=result['discovery_s']/4,
                       remote_stage_mean_s=sum(x['remote_stage_s'] for x in iterations)/4,
                       coordinator_and_local_mean_s=(result['discovery_s']-sum(x['remote_stage_s'] for x in iterations))/4,
                       remote_trajectory_mib=fetched_bytes/2**20,remote_read_mib=remote_read/2**20,
                       remote_write_mib=remote_write/2**20,remote_requests=remote_ops,
                       coordinator_read_mib=result['coordinator_protocol']['read_bytes']/2**20,
                       coordinator_write_mib=result['coordinator_protocol']['write_bytes']/2**20,
                       modeled_latency_ns_sum=model_latency_ns,private_cache_peak_mib=copy_peak/2**20,
                       reserved_arena_mib=result['allocated_address_bytes']/2**20,
                       live_payload_peak_mib=max(x['live_payload_bytes'] for x in iterations)/2**20,
                       validation_nrmse=iterations[-1]['validation_nrmse'],setup_s=result['setup_s'])
            rows.append(row)
            groups[(cfg['kernel'],cfg['n'],cfg['layout'],cfg['policy'])].append(row)
            results.append(dict(path=str(trial.relative_to(root)),sha256=hashlib.sha256((trial/'result.json').read_bytes()).hexdigest()))
    assert rows and not failures, ('no rows or failed trials',failures)
    for key,values in exact.items():
        assert all(v[1]==values[0][1] for v in values), ('policy output mismatch',key)
    # Different NumPy/LAPACK versions need not give bitwise-identical solves.
    # Candidate equality and a predeclared tolerance are separate requirements.
    max_error = 0.
    for key,values in numerical.items():
        reference = values[0][1]
        for _,runs in values:
            for a,b in zip(reference,runs):
                assert a[2] == b[2], ('placement changed candidate selection',key)
                for x,y in zip(a[:2],b[:2]):
                    assert np.allclose(x,y,rtol=1e-3,atol=1e-4), ('cross-host numerical mismatch',key)
                    max_error = max(max_error,float(np.max(np.abs(x-y))))
    analysis = root/'analysis'; analysis.mkdir(exist_ok=True)
    with (analysis/'trials.csv').open('w') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    aggregates=[]
    for key,values in sorted(groups.items()):
        row = dict(zip(['kernel','n','layout','policy'],key),blocks=len(values))
        for metric in ['iteration_mean_s','remote_stage_mean_s','coordinator_and_local_mean_s',
                       'remote_trajectory_mib','private_cache_peak_mib','coordinator_read_mib','coordinator_write_mib']:
            vals=[v[metric] for v in values]
            for suffix,fn in [('median',statistics.median),('min',min),('max',max)]:
                row[metric+'_'+suffix]=fn(vals)
        aggregates.append(row)
    with (analysis/'summary.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(aggregates[0])); writer.writeheader(); writer.writerows(aggregates)
    paired=[]
    for key,values in sorted(groups.items()):
        kernel,n,layout,policy=key
        if policy=='refetch' or (kernel,n,layout,'refetch') not in groups: continue
        base={v['seed']:v for v in groups[(kernel,n,layout,'refetch')]}
        ratios=[base[v['seed']]['discovery_s']/v['discovery_s'] for v in values]
        rng=np.random.default_rng(1907)
        boot=np.median(rng.choice(ratios,(10000,len(ratios)),replace=True),axis=1)
        paired.append(dict(kernel=kernel,n=n,layout=layout,policy=policy,baseline='refetch',
                           ratios=ratios,median=statistics.median(ratios),bootstrap95=np.quantile(boot,[.025,.975]).tolist()))
    validation=dict(status='pass',loops=len(rows),iterations=4*len(rows),failures=failures,
                    exact_policy_groups=sum(len(v)>1 for v in exact.values()),
                    exact_groups_total=len(exact),
                    cross_placement_groups=sum(len({r['layout'] for r in rows if (r['kernel'],r['n'],r['seed'])==key})>1
                                               for key in numerical),
                    cross_placement_max_absolute_error=max_error,numerical_tolerance=dict(rtol=1e-3,atol=1e-4),
                    source_hashes=source_hashes,result_hashes=results,
                    caveat='Wall time measures this TCP simulator, including SSH. Modeled latency sums are not predicted E2E time.')
    for name,value in [('validation',validation),('contrasts',paired)]:
        (analysis/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps({k:validation[k] for k in ['status','loops','iterations','exact_policy_groups','cross_placement_max_absolute_error']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('root',type=Path)
    analyze(parser.parse_args().root.resolve())
