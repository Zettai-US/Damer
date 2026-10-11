#!/usr/bin/env python3
"""Validate paper evidence against the experiment design and source provenance."""
from pathlib import Path
from collections import Counter
import hashlib,json
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'experiments/results'
def read(n):return [json.loads(x) for x in (D/f'{n}.jsonl').read_text().splitlines()]

def main():
    f,p,c=read('fanout'),read('phases'),read('compiler')
    assert (len(f),len(p),len(c))==(2250,384,500)
    assert set(Counter((x['workers'],x['bytes'],x['scans'],x['policy']) for x in f).values())=={25}
    assert set(Counter((x['bytes'],x['gap_us'],x['policy'],x['hint_accuracy_pct']) for x in p).values())=={12}
    assert set(Counter(x['nodes'] for x in c).values())=={100}
    sha=hashlib.sha256((ROOT/'experiments/cxl_bench.c').read_bytes()).hexdigest()
    manifest=json.loads((D/'native_manifest.json').read_text())
    assert sha==manifest['source_sha256'] and not manifest['skipped_for_memory_margin']
    for x in f:
        assert x['source_sha256']==sha and x['checksum_ok'] and x['duration_ns']>0
        pages=x['workers']*x['bytes']//4096
        if x['policy']=='share':assert (x['mapped_pages_node0'],x['mapped_pages_node1'])==(0,pages)
        else:assert (x['mapped_pages_node0'],x['mapped_pages_node1'])==(pages,0)
        if x['payload_pss_kib']>=0:
            expected=x['bytes']//1024*(1 if x['policy']=='share' else x['workers'])
            assert abs(x['payload_pss_kib']-expected)<=x['workers']+1
    for x in p:
        assert x['checksum_ok'] and x['local_pages_node0']==x['bytes']//4096
        copies=x['steps'] if x['policy']=='demand' else x['steps']*(2-x['hint_accuracy_pct']/100)
        assert x['bytes_copied']==copies*x['bytes']
        if x['policy']=='lookahead':assert x['hint_hits']==x['steps']*x['hint_accuracy_pct']/100
    for x in c:
        assert x['unique_inputs']==1 and x['output_delta_bytes']==4096
        assert x['reserved_bytes']==4096*(x['nodes']+1)
    assert json.loads((D/'hardware.json').read_text())['all_node1_blocks_inside_decoder']
    print('PASS: 2,250 native fanout observations, 384 phase episodes, 500 compiler trials.')
    print('PASS: source provenance, full grids, placement, PSS accounting, checksums and copy volumes.')

if __name__=='__main__':main()
