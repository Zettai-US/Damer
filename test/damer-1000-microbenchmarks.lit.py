# RUN: python3 -B %s | tee /dev/stderr | FileCheck %s

#!/usr/bin/env python3

import json
import os
import statistics
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import damer_compile  # noqa: E402


TRANSFORM_CASES = [
    [],
    ["quantize"],
    ["compress"],
    ["checksum"],
    ["filter"],
    ["reduce"],
    ["scatter/gather"],
    ["replicate"],
    ["persist"],
]

NODE_CASES = [
    ("host_memory", 'memref<?xi8, "host">'),
    ("cxl_memory_device", 'memref<?xi8, "cxl">'),
    ("gpu_memory", 'memref<?xi8, "gpu">'),
    ("accelerator", 'memref<?xi8, "accelerator">'),
    ("switch_compute_engine", 'memref<?xi8, "bluefield">'),
]

ORDERING_CASES = ["none", "program_order", "acquire_release", "total"]
OWNERSHIP_CASES = ["borrowed", "source", "destination", "shared"]


def percentile(values, pct):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def make_event(index):
    source_node, source_type = NODE_CASES[index % len(NODE_CASES)]
    destination_node, destination_type = NODE_CASES[(index // len(NODE_CASES)) % len(NODE_CASES)]
    transforms = TRANSFORM_CASES[index % len(TRANSFORM_CASES)]
    actions = ["move"] + transforms
    ordering = ORDERING_CASES[index % len(ORDERING_CASES)]
    ownership = OWNERSHIP_CASES[(index // 3) % len(OWNERSHIP_CASES)]

    edge = {
        "bytes": 4096 * ((index % 257) + 1),
        "stride": 1 << (index % 4),
        "reuse_distance": index % 1024,
        "read_write_ratio": f"{(index % 4) + 1}:{((index // 4) % 4) + 1}",
        "ordering_requirement": ordering,
        "alias_set": f"micro_alias_{index % 31}",
        "ownership": ownership,
        "priority": index % 11,
        "ttl": (index % 8) + 1,
    }
    if "scatter/gather" in transforms:
        edge["fragments"] = (index % 8) + 1
    if "replicate" in transforms:
        edge["fanout"] = (index % 4) + 2

    return {
        "switchlet": f"micro_{index:04d}",
        "source": {
            "name": "src",
            "type": source_type,
            "node": source_node,
        },
        "destination": {
            "name": "dst",
            "type": destination_type,
            "node": destination_node,
        },
        "actions": actions,
        "edge": edge,
    }


def main():
    case_count = 1000
    compile_us = []
    bpf_c_us = []
    kinds = set()
    placements = set()
    nodes = set()
    max_rdma_ops = 0
    total_bpf_c_bytes = 0

    for index in range(case_count):
        event = make_event(index)
        start = time.perf_counter_ns()
        switchlet = damer_compile.parse_json_switchlet(event)
        plan = damer_compile.compile_plan(switchlet, damer_compile.CompileOptions())
        compile_us.append((time.perf_counter_ns() - start) / 1000.0)

        if not plan["verifier"]["ok"]:
            print(json.dumps(plan["verifier"], indent=2, sort_keys=True))
            raise AssertionError(f"verifier rejected case {index}")

        edge = plan["edges"][0]
        kinds.add(edge["kind"])
        placements.add(edge["placement"])
        nodes.add(edge["source"]["node"])
        nodes.add(edge["destination"]["node"])
        max_rdma_ops = max(max_rdma_ops, edge["effect"]["max_rdma_ops"])

        props = edge["properties"]
        assert props["bytes"] == event["edge"]["bytes"]
        assert props["stride"] == event["edge"]["stride"]
        assert props["reuse_distance"] == event["edge"]["reuse_distance"]
        assert props["ordering_requirement"] == event["edge"]["ordering_requirement"]
        assert props["ownership"] == event["edge"]["ownership"]
        assert edge["source"]["node"] == event["source"]["node"]
        assert edge["destination"]["node"] == event["destination"]["node"]

        start = time.perf_counter_ns()
        bpf_c = damer_compile.compile_bpf_c(plan)
        bpf_c_us.append((time.perf_counter_ns() - start) / 1000.0)
        total_bpf_c_bytes += len(bpf_c)
        assert "damer_emit_edge" in bpf_c
        assert "damer_submit_decision" in bpf_c
        assert "struct damer_edge_event" in bpf_c

    expected_kinds = {
        "move",
        "move+quantize",
        "move+compress",
        "move+checksum",
        "move+filter",
        "move+reduce",
        "move+scatter_gather",
        "move+replicate",
        "move+persist",
    }
    expected_nodes = {node for node, _ in NODE_CASES}
    assert expected_kinds.issubset(kinds), sorted(expected_kinds - kinds)
    assert expected_nodes.issubset(nodes), sorted(expected_nodes - nodes)

    print(
        "MICROBENCH cases={cases} ok=true kinds={kinds} nodes={nodes} placements={placements} max_rdma_ops={max_rdma_ops}".format(
            cases=case_count,
            kinds=len(kinds),
            nodes=len(nodes),
            placements=len(placements),
            max_rdma_ops=max_rdma_ops,
        )
    )
    print(
        "MICROBENCH_TIMING compile_median_us={compile_median:.3f} compile_p95_us={compile_p95:.3f} bpf_c_median_us={bpf_c_median:.3f} bpf_c_p95_us={bpf_c_p95:.3f} bpf_c_total_bytes={bpf_c_total_bytes}".format(
            compile_median=statistics.median(compile_us),
            compile_p95=percentile(compile_us, 0.95),
            bpf_c_median=statistics.median(bpf_c_us),
            bpf_c_p95=percentile(bpf_c_us, 0.95),
            bpf_c_total_bytes=total_bpf_c_bytes,
        )
    )


if __name__ == "__main__":
    main()

# CHECK: MICROBENCH cases=1000 ok=true kinds=9 nodes=5
# CHECK: MICROBENCH_TIMING compile_median_us=
