#!/usr/bin/env python3
"""Nonoverlapping coordinator-clock accounting for the controller ablation."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics


def main(root):
    audit=json.loads((root/'analysis/validation.json').read_text());assert audit['status']=='pass'
    rows=[];groups=defaultdict(dict)
    for name,digest in audit['result_hashes'].items():
        path=root/name/'result.json';assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        d=json.loads(path.read_text());c=d['config']
        if d['schema']!='science-memsim-v1':continue
        protocol=d['coordinator_protocol'];remote=sum(x['remote_stage_s'] for x in d['iterations'])
        other=d['discovery_s']-protocol['io_s']-remote
        assert other>=0,('overlapping timing scopes',name,other)
        row=dict(trial=name,kernel=c['kernel'],policy=c['policy'],controller=c['controller'],seed=c['seed'],
            iteration_s=d['discovery_s']/4,coordinator_protocol_s=protocol['io_s']/4,
            remote_stage_s=remote/4,local_other_s=other/4,coordinator_requests=protocol['requests'],
            coordinator_payload_bytes=protocol['read_bytes']+protocol['write_bytes'])
        rows.append(row);groups[(c['kernel'],c['policy'],c['seed'])][c['controller']]=row
    assert rows
    pairs=defaultdict(list)
    for key,values in groups.items():
        assert set(values)=={'barrier','damer'}
        a,b=values['damer'],values['barrier']
        pairs[key[:2]].append(dict(seed=key[2],delta_iteration_s=a['iteration_s']-b['iteration_s'],
            delta_protocol_s=a['coordinator_protocol_s']-b['coordinator_protocol_s'],
            delta_remote_s=a['remote_stage_s']-b['remote_stage_s'],
            delta_local_other_s=a['local_other_s']-b['local_other_s'],
            extra_coordinator_requests=a['coordinator_requests']-b['coordinator_requests'],
            extra_coordinator_payload_bytes=a['coordinator_payload_bytes']-b['coordinator_payload_bytes']))
    summaries=[]
    for key,values in sorted(pairs.items()):
        summaries.append(dict(kernel=key[0],policy=key[1],pairs=len(values),
            **{k+'_median':statistics.median(v[k] for v in values) for k in values[0] if k!='seed'},observations=values))
    out=root/'analysis'
    with (out/'controller_profile.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    report=dict(status='pass',loops=len(rows),contrasts=summaries,
        timing='Per-iteration means; protocol I/O and remote-stage wait are nonoverlapping on the coordinator clock. Remainder includes numerics, serialization and trace work.',
        limits='Protocol I/O includes both control and bulk payloads. Paired time differences include transport noise. Byte counts exclude protocol framing and are not NIC or CXL-fabric counters.',
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        input_audit_sha256=hashlib.sha256((out/'validation.json').read_bytes()).hexdigest())
    (out/'controller_profile.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status='pass',profiled_loops=len(rows),paired_groups=len(groups))))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);main(p.parse_args().root.resolve())
