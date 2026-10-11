#!/usr/bin/env python3
"""Export measured object effects for a future REAL Sentry adapter run.

This is a vendor-neutral experiment input, not an NVIDIA API implementation.
There is deliberately no latency simulation or baseline speedup calculation.
"""
import argparse
import hashlib
import json
from pathlib import Path

from store import dump


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('suite',type=Path)
    a=p.parse_args();root=a.suite
    out=root/'sentry-comparison';out.mkdir(exist_ok=False)
    rows=[]
    for line in (root/'episodes.jsonl').read_text().splitlines():
        episode=json.loads(line)['episode']
        events=[json.loads(s) for f in (root/episode/'store/events').glob('*.jsonl') for s in f.read_text().splitlines()]
        start=min(e['request_ns'] for e in events)
        for e in sorted(events,key=lambda x:x['request_ns']):
            if e['kind']!='opencode-live':continue
            rows.append(dict(episode=episode,event_id=e['event_id'],agent=e['agent'],
                object=e['object'],version=e.get('version_read'),operation=e['op'],
                offset=0,length=e.get('logical_bytes_read',0),arrival_offset_ns=e['request_ns']-start,
                expected_decision='allow',source_status=e['status']))
    replay=out/'requests.jsonl'
    replay.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    contract=dict(schema='arvm-sentry-comparison-input-v1',status='awaiting_actual_sentry_backend',
        requests=len(rows),request_sha256=hashlib.sha256(replay.read_bytes()).hexdigest(),
        semantics='Grant/check/release/revoke of scoped shared-object access; declare unsupported operations explicitly',
        arrival_modes=['original offsets for behavior replay','saturated per-agent order for performance; preserve data dependencies'],
        required_baseline_provenance=['Sentry and DOCA versions','BlueField generation/firmware',
            'host hardware and topology','actual policy and identity mapping','transport/API and timing boundary',
            'raw attempt-level decisions and latency including failures'],
        performance_metrics=['grant issue latency','grant check + pin/release latency','revoke acknowledgement latency',
            'deny-after-revoke result','throughput','CPU/DPU utilization','full task success and end-to-end latency'],
        controls=['Same accepted task graph and object versions','Same model and prompts for end-to-end trials',
            'Same concurrency and object sizes','Separate cold startup and warm path','Preserve all failed attempts',
            'Report trust-domain and enforcement differences; local MCP grants are not DPU pointer enforcement'],
        not_allowed=['Replacing Sentry with OpenShell, gVisor Sentry, sleep delays or a local mock',
            'Turning vendor quarantine-time claims into grant latency','Claiming a relative speedup without a matched measured baseline'],
        primary_sources=['https://www.nvidia.com/en-us/solutions/ai/agent-safety/',
            'https://nvidianews.nvidia.com/news/open-agent-safety-platform'])
    dump(out/'protocol.json',contract)
    print(json.dumps(dict(status=contract['status'],requests=len(rows))))


if __name__=='__main__':main()
