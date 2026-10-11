#!/usr/bin/env python3
"""Validate measured CXL sharing, retention ablations and observed dataflow."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from dataflow import analyze_run,interval_union
from store import dump


def lines(path):return [json.loads(line) for line in Path(path).read_text().splitlines()]


def analyze_profile(root):
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
    for name,digest in manifest['source_sha256'].items():
        assert hashlib.sha256((root/'source'/name).read_bytes()).hexdigest()==digest, name
    observed=analyze_run(root)
    events=[e for f in (root/'store/events').glob('*.jsonl') for e in lines(f) if e['kind']=='opencode-live']
    good=sorted((e for e in events if e['status']=='ok'),key=lambda e:e['acquired_ns'])
    memory=lines(root/'memory.jsonl');initial=memory[0]
    state=json.loads((root/'store/dsec.json').read_text())
    config=json.loads((root/'store/store.json').read_text())
    lifecycle=[e for f in (root/'store').glob('dsec-*.jsonl') for e in lines(f)]
    by_version=defaultdict(list);by_object=defaultdict(list)
    for e in good:
        obj=initial['objects'][e['object']]
        assert e['backing_inode']==obj['inode'] and e['backing_device']==obj['device']
        assert e['nodes']=={'1':config['capacity']//4096} and e['lock_nodes']=={'1':1}
        by_object[e['object']].append(e)
        if e['op']=='read':by_version[(e['object'],e['version_read'])].append(e)
    assert len({(o['device'],o['inode']) for o in initial['objects'].values()})==len(initial['objects'])
    # A task is finished only after its OpenCode/MCP namespace has exited.
    finished={e['agent']:e['ns'] for e in lifecycle if e['event']=='finish'}
    pins={e['token']:e for e in lifecycle if e['event']=='pin'}
    unpins={e['token']:e for e in lifecycle if e['event']=='unpin'}
    roots_released={e['object']:e['ns'] for e in lifecycle if e['event']=='release_root'}
    assert set(pins)==set(unpins)
    for e in lifecycle:
        if e['event']!='reclaim':continue
        if e['object'] in initial['session_roots']:
            assert roots_released[e['object']]<=e['ns']
        for task,spec in state['sandboxes'].items():
            uses=spec.get('effects',spec['grants']) if manifest['retention_policy']=='dataflow' else spec['grants']
            if any(e['object'] in ids for ids in uses.values()):assert finished[task]<=e['ns']
        for token,pin in pins.items():
            if pin['object']==e['object']:assert unpins[token]['ns']<=e['ns']
        assert not (Path(config['payload_root'])/e['object']).exists()
    assert not state['pins']
    if manifest['status']=='complete':assert memory[-1]['payload_bytes']==0
    integral=sum(a['payload_bytes']*(b['ns']-a['ns'])/1e9 for a,b in zip(memory,memory[1:]))
    controls=sum(a['control_bytes']*(b['ns']-a['ns'])/1e9 for a,b in zip(memory,memory[1:]))
    window=(memory[-1]['ns']-memory[0]['ns'])/1e9
    # Replay the conservative scope-only rule over exactly this observed task
    # timeline. It is a byte-time counterfactual, not another timed execution.
    last_task=max(finished.values())
    release_root=next(e['ns'] for e in lifecycle if e['event']=='release_root' and e['object']=='answer')
    replay_scope=initial['payload_bytes']*(last_task-initial['ns'])/1e9+config['capacity']*max(0,release_root-last_task)/1e9
    version_rows=[]
    reads=[e for e in good if e['op']=='read' and e['logical_bytes_read']>0]
    shared=0
    for (obj,version),accesses in sorted(by_version.items()):
        actors=sorted({e['actor'] for e in accesses});tasks=sorted({e['agent'] for e in accesses})
        valid=[e for e in accesses if e['logical_bytes_read']>0]
        if len(actors)>1:shared+=len(valid)
        version_rows.append(dict(object=obj,version=version,actors=actors,tasks=tasks,read_count=len(accesses),
            logical_bytes=accesses[0]['logical_bytes_read'],first_ns=accesses[0]['acquired_ns'],
            last_ns=max(e['released_ns'] for e in accesses),
            active_ns=interval_union((e['acquired_ns'],e['released_ns']) for e in accesses)))
    corpus=next(r for r in version_rows if r['object']=='corpus')
    # Idle-time-only hypothetical reclamation: count reads preceded by a gap
    # greater than TTL since the previous access. No eviction is executed.
    ttl=[]
    for threshold in [.1,.5,1.,2.,5.]:
        at_risk=[]
        for obj,accesses in by_object.items():
            for previous,current in zip(accesses,accesses[1:]):
                if current['op']=='read' and current['logical_bytes_read']>0:
                    gap=current['acquired_ns']-previous['released_ns']
                    if gap>threshold*1e9:at_risk.append(dict(object=obj,event_id=current['event_id'],gap_ns=gap,
                        consumer=current['agent'],version=current['version_read']))
        ttl.append(dict(ttl_s=threshold,reads_after_expiry=len(at_risk),objects=len({e['object'] for e in at_risk}),events=at_risk))
    readers={e['agent'] for e in good if e['object']=='corpus' and e['op']=='read'}
    task_phases={name:spec.get('phase') for name,spec in state['sandboxes'].items()}
    flow=[]
    for e in good:
        if e['op']=='read' and e['producer']!='harness' and e['producer']!=e['agent']:
            flow.append(dict(producer=e['producer'],consumer=e['agent'],object=e['object'],version=e['version_read'],
                source_phase=task_phases[e['producer']],destination_phase=task_phases[e['agent']]))
    tokens=[]
    for task in state['sandboxes']:
        for event in lines(root/task/'opencode.jsonl'):
            if event.get('type')=='step_finish':tokens.append(event['part'].get('tokens',{}))
    report=dict(schema='arvm-cxl-profile-analysis-v1',status=manifest['status'],protocol_complete=manifest['protocol_complete'],
        workers=manifest['workers'],policy=manifest['retention_policy'],events=len(events),
        task_sessions=manifest['task_sessions'],logical_actors=manifest['workers']+1,
        successful_events=len(good),errors=sum(e['status']=='error' for e in events),
        nonempty_reads=len(reads),shared_nonempty_reads=shared,shared_read_ratio=shared/len(reads) if reads else None,
        cross_task_reads=len(flow),data_dependencies=flow,version_rows=version_rows,
        compile_us=manifest['compile_ns']/1000,e2e_s=manifest['duration_ns']/1e9,
        allocated_payload_kib=initial['payload_bytes']/1024,initial_control_kib=initial['control_bytes']/1024,
        profile_window_s=window,payload_mib_seconds=integral/2**20,control_mib_seconds=controls/2**20,
        average_payload_kib=integral/window/1024,
        same_timeline_scope_payload_mib_seconds=replay_scope/2**20,
        same_timeline_payload_reduction=1-integral/replay_scope,
        reclaimed_payload_objects=sum(e['event']=='reclaim' for e in lifecycle),
        corpus=dict(backing_allocations=1,task_mappings=len(readers),logical_readers=len(corpus['actors']),
            allocated_bytes=config['capacity'],logical_bytes=corpus['logical_bytes'],
            observed_span_s=(corpus['last_ns']-corpus['first_ns'])/1e9,active_access_ms=corpus['active_ns']/1e6,
            alias_verified_by='same device+inode across task processes, MAP_SHARED, and actual CXL residency'),
        idle_expiry_replay=ttl,
        output_tokens=sum(t.get('output',0) for t in tokens),lock_contended=observed['metrics']['lock_contended'],
        native_lock_wait_total_ns=sum(e.get('native_wait_ns',0) for e in good),
        checks=['source snapshot hashes','versioned producer-consumer linkage','actual CXL data and lock pages',
            'same backing inode across task mappings','no reclaim before future task or pin drain',
            'session roots released before reclaim; reclaimed backing absent',
            'all payload files reclaimed on success','logical actor versus task-instance separation'],
        limitations=['Fixed accepted review graph, not prevalence of natural agent behavior',
            '64 KiB payload slots include unused output capacity; control state and LLM memory reported separately',
            'Scope replay is a capacity-time counterfactual; idle replay is future-access risk, not observed crashes',
            'Graph information can be represented by equivalent reference/effect tables; this does not prove a unique representation',
            'Model tool responses are copied; shared mappings belong to trusted tool processes',
            'Sampled whole-episode latency includes model/server startup; no causal inference-speedup claim'])
    dump(root/'profile.json',report)
    return report
