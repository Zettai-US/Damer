#!/usr/bin/env python3

import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import damer_compile  # noqa: E402


def compile_fixture(path):
    switchlet = damer_compile.load_switchlet(os.path.join(ROOT, path))
    return damer_compile.compile_plan(switchlet, damer_compile.CompileOptions())


def test_dsl_kv_pack():
    plan = compile_fixture("examples/damer/kv_pack.damer")
    assert plan["switchlet"] == "kv_pack"
    assert plan["verifier"]["ok"]
    assert len(plan["edges"]) == 1

    edge = plan["edges"][0]
    assert edge["kind"] == "move+quantize"
    assert edge["source"]["node"] == "cxl_memory_device"
    assert edge["destination"]["node"] == "cxl_memory_device"
    assert edge["placement"] == "switch_compute_engine"


def test_json_event_kv_pack():
    plan = compile_fixture("examples/damer/kv_pack.event.json")
    edge = plan["edges"][0]
    assert edge["kind"] == "move+quantize"
    assert edge["properties"]["alias_set"] == "kv"
    assert edge["properties"]["ownership"] == "destination"

    c_source = damer_compile.compile_bpf_c(plan)
    assert 'SEC("damer/switchlet/kv_pack")' in c_source
    assert "damer_emit_edge" in c_source
    assert "damer_submit_decision" in c_source
    assert "DAMER_TRANSFORM_QUANTIZE" in c_source


def test_cli_plan_json_round_trip():
    plan = compile_fixture("examples/damer/kv_pack.event.json")
    encoded = json.dumps(plan)
    decoded = json.loads(encoded)
    assert decoded["kind"] == "damer.ebpf.middleware.plan"
    assert decoded["bpftime"]["runtime"] == "bpftime"


def compile_event(actions, source_node="cxl_memory_device", destination_node="gpu_memory"):
    switchlet = damer_compile.parse_json_switchlet(
        {
            "switchlet": "matrix_case",
            "source": {
                "name": "src",
                "type": 'memref<?xi8, "cxl">',
                "node": source_node,
            },
            "destination": {
                "name": "dst",
                "type": 'memref<?xi8, "gpu">',
                "node": destination_node,
            },
            "actions": actions,
            "edge": {
                "bytes": 4096,
                "stride": 1,
                "reuse_distance": 8,
                "read_write_ratio": "2:1",
                "ordering_requirement": "acquire_release",
                "alias_set": "kv",
                "ownership": "shared",
                "ttl": 4,
            },
        }
    )
    return damer_compile.compile_plan(switchlet, damer_compile.CompileOptions())


def test_action_matrix():
    cases = [
        (["move"], "move", "DAMER_TRANSFORM_NONE"),
        (["move", "quantize"], "move+quantize", "DAMER_TRANSFORM_QUANTIZE"),
        (["move", "compress"], "move+compress", "DAMER_TRANSFORM_COMPRESS"),
        (["move", "checksum"], "move+checksum", "DAMER_TRANSFORM_CHECKSUM"),
        (["move", "filter"], "move+filter", "DAMER_TRANSFORM_FILTER"),
        (["move", "reduce"], "move+reduce", "DAMER_TRANSFORM_REDUCE"),
        (["move", "scatter/gather"], "move+scatter_gather", "DAMER_TRANSFORM_SCATTER_GATHER"),
        (["move", "replicate"], "move+replicate", "DAMER_TRANSFORM_REPLICATE"),
        (["move", "persist"], "move+persist", "DAMER_TRANSFORM_PERSIST"),
    ]

    for actions, expected_kind, expected_flag in cases:
        plan = compile_event(actions)
        assert plan["verifier"]["ok"]
        edge = plan["edges"][0]
        assert edge["kind"] == expected_kind
        assert edge["source"]["node"] == "cxl_memory_device"
        assert edge["destination"]["node"] == "gpu_memory"
        assert edge["properties"]["bytes"] == 4096
        assert edge["properties"]["stride"] == 1
        assert edge["properties"]["reuse_distance"] == 8
        c_source = damer_compile.compile_bpf_c(plan)
        assert expected_flag in c_source


def test_node_matrix():
    node_types = {
        "host_memory": 'memref<?xi8, "host">',
        "cxl_memory_device": 'memref<?xi8, "cxl">',
        "gpu_memory": 'memref<?xi8, "gpu">',
        "accelerator": 'memref<?xi8, "accelerator">',
        "switch_compute_engine": 'memref<?xi8, "bluefield">',
    }
    for expected, typ in node_types.items():
        assert damer_compile.classify_node(typ) == expected


if __name__ == "__main__":
    test_dsl_kv_pack()
    test_json_event_kv_pack()
    test_cli_plan_json_round_trip()
    test_action_matrix()
    test_node_matrix()
