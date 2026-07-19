#!/usr/bin/env python3
"""Deterministic fuzzer for the Damer eBPF middleware compiler."""

import argparse
import copy
import json
import os
import random
import statistics
import sys
import time
import traceback
import zlib
from typing import Dict, List, Sequence, Tuple

import damer_compile


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

TRANSFORMS = [
    "quantize",
    "compress",
    "checksum",
    "filter",
    "reduce",
    "scatter/gather",
    "replicate",
    "persist",
]

NODE_CASES = [
    ("host_memory", 'memref<?xi8, "host">'),
    ("cxl_memory_device", 'memref<?xi8, "cxl">'),
    ("gpu_memory", 'memref<?xi8, "gpu">'),
    ("accelerator", 'memref<?xi8, "accelerator">'),
    ("switch_compute_engine", 'memref<?xi8, "bluefield">'),
]

ORDERING_CASES = ["none", "program_order", "program", "acquire_release", "total"]
OWNERSHIP_CASES = ["unknown", "borrowed", "source", "destination", "shared"]


def parse_seed(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError:
        return zlib.crc32(value.encode("utf-8"))


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def choose_transforms(index: int, rng: random.Random) -> List[str]:
    if index < len(TRANSFORM_CASES) * 8:
        return list(TRANSFORM_CASES[index % len(TRANSFORM_CASES)])
    count = rng.randint(0, min(4, len(TRANSFORMS)))
    return rng.sample(TRANSFORMS, count)


def make_edge_attrs(index: int, rng: random.Random, transforms: Sequence[str]) -> Dict[str, object]:
    attrs: Dict[str, object] = {
        "bytes": rng.choice([0, 64, 128, 4096, 65536, 1048576, (index % 257 + 1) * 4096]),
        "stride": rng.choice([1, 2, 4, 8, "contiguous", "dynamic"]),
        "reuse_distance": rng.choice([0, 1, 8, 64, 512, "unknown", index % 2048]),
        "read_write_ratio": f"{rng.randint(1, 8)}:{rng.randint(1, 8)}",
        "ordering_requirement": rng.choice(ORDERING_CASES),
        "alias_set": rng.choice(["unknown", "kv", "activation", "checkpoint", f"alias_{index % 97}"]),
        "ownership": rng.choice(OWNERSHIP_CASES),
        "priority": rng.randint(0, 15),
        "ttl": rng.randint(1, 16),
    }
    if "scatter/gather" in transforms:
        attrs["fragments"] = rng.randint(1, 8)
    if "replicate" in transforms:
        attrs["fanout"] = rng.randint(2, 8)
    if rng.random() < 0.10:
        attrs["redirect"] = True
    if rng.random() < 0.10:
        attrs["retry"] = True
    return attrs


def make_action_event(index: int, rng: random.Random) -> Dict[str, object]:
    source_node, source_type = NODE_CASES[index % len(NODE_CASES)]
    destination_node, destination_type = NODE_CASES[(index // len(NODE_CASES)) % len(NODE_CASES)]
    transforms = choose_transforms(index, rng)
    source = {"name": "src", "type": source_type}
    destination = {"name": "dst", "type": destination_type}
    if rng.random() < 0.65:
        source["node"] = source_node
    if rng.random() < 0.65:
        destination["node"] = destination_node
    return {
        "switchlet": f"fuzz_action_{index:06d}",
        "source": source,
        "destination": destination,
        "actions": ["move"] + transforms,
        "edge": make_edge_attrs(index, rng, transforms),
    }


def make_pipeline_event(index: int, rng: random.Random) -> Dict[str, object]:
    edge_count = 1 + (index % 3)
    args = []
    pipeline = []

    for edge_index in range(edge_count):
        source_node, source_type = NODE_CASES[(index + edge_index) % len(NODE_CASES)]
        destination_node, destination_type = NODE_CASES[
            (index + edge_index * 3) % len(NODE_CASES)
        ]
        source = {
            "name": f"src{edge_index}",
            "type": source_type,
            "node": source_node,
        }
        destination = {
            "name": f"dst{edge_index}",
            "type": destination_type,
            "node": destination_node,
        }
        args.extend([source, destination])

        transforms = choose_transforms(index + edge_index, rng)
        attrs = make_edge_attrs(index + edge_index, rng, transforms)
        current = f"v{edge_index}_0"
        pipeline.append(
            {
                "op": "read",
                "result": current,
                "operands": [source["name"]],
                "attrs": attrs,
            }
        )
        for transform_index, transform in enumerate(transforms, start=1):
            result = f"v{edge_index}_{transform_index}"
            pipeline.append(
                {
                    "op": transform,
                    "result": result,
                    "operands": [current],
                    "attrs": attrs,
                }
            )
            current = result
        pipeline.append(
            {
                "op": "write",
                "operands": [current, destination["name"]],
                "attrs": attrs,
            }
        )

    return {
        "switchlet": f"fuzz_pipeline_{index:06d}",
        "args": args,
        "pipeline": pipeline,
    }


def make_dsl_text(index: int, rng: random.Random) -> str:
    _, source_type = NODE_CASES[index % len(NODE_CASES)]
    _, destination_type = NODE_CASES[(index // len(NODE_CASES)) % len(NODE_CASES)]
    transforms = choose_transforms(index, rng)
    lines = [
        f"damer.switchlet @fuzz_dsl_{index:06d}(",
        f"    %src : {source_type},",
        f"    %dst : {destination_type}) {{",
        "  %v0 = damer.read %src",
    ]
    current = "v0"
    for transform_index, transform in enumerate(transforms, start=1):
        normalized = transform.replace("/", "_")
        result = f"v{transform_index}"
        lines.append(f"  %{result} = damer.{normalized} %{current}")
        current = result
    lines.append(f"  damer.write %{current}, %dst")
    lines.append("}")
    return "\n".join(lines)


def make_valid_case(index: int, rng: random.Random) -> Tuple[str, object]:
    if index % 11 == 0:
        return "dsl", make_dsl_text(index, rng)
    if index % 7 == 0:
        return "json", make_pipeline_event(index, rng)
    return "json", make_action_event(index, rng)


def mutate_invalid(kind: str, payload: object, rng: random.Random) -> Tuple[str, object]:
    if kind == "dsl":
        text = str(payload)
        mutations = [
            lambda value: value.replace("damer.switchlet", "damer.broken", 1),
            lambda value: value.replace("damer.write", "damer.unknown_write", 1),
            lambda value: value.replace("%src", "%missing", 1),
            lambda value: value + "\n  damer.write %x, %dst",
        ]
        return kind, rng.choice(mutations)(text)

    event = copy.deepcopy(payload)
    if not isinstance(event, dict):
        return kind, 42

    mutations = [
        lambda value: value.pop("source", None),
        lambda value: value.update({"source": 17}),
        lambda value: value.update({"args": "not-a-list"}),
        lambda value: value.update({"actions": "move"}),
        lambda value: value.update({"edge": "not-an-object"}),
        lambda value: value.update({"pipeline": [{"op": "write", "operands": ["dangling", "dst"]}]}),
        lambda value: value.update({"pipeline": [{"result": "v0", "operands": ["src"]}]}),
        lambda value: value.update({"pipeline": "not-a-list"}),
        lambda value: value.update({"edge": {"retry": True, "ttl": 0}}),
        lambda value: value.update(
            {
                "actions": ["move", "replicate"],
                "edge": {"fanout": 4096, "ttl": 1},
            }
        ),
    ]
    rng.choice(mutations)(event)
    return kind, event


def compile_payload(kind: str, payload: object, options: damer_compile.CompileOptions) -> Dict[str, object]:
    if kind == "dsl":
        switchlet = damer_compile.parse_damer_dsl(str(payload))
    else:
        switchlet = damer_compile.parse_json_switchlet(payload)
    return damer_compile.compile_plan(switchlet, options)


def assert_plan_invariants(plan: Dict[str, object]) -> None:
    assert plan["kind"] == "damer.ebpf.middleware.plan"
    assert plan["frontend"] == "bpftime"
    assert isinstance(plan["nodes"], list)
    assert isinstance(plan["edges"], list)
    assert plan["edges"]
    assert "helpers" in plan["bpftime"]
    assert "damer_emit_edge" in plan["bpftime"]["helpers"]

    for edge in plan["edges"]:
        assert edge["actions"][0] == "move"
        assert edge["source"]["node"] in damer_compile.NODE_ENUMS
        assert edge["destination"]["node"] in damer_compile.NODE_ENUMS
        assert edge["placement"] in damer_compile.NODE_ENUMS
        assert edge["effect"]["max_rdma_ops"] >= 1
        props = edge["properties"]
        assert "bytes" in props
        assert "stride" in props
        assert "ordering_requirement" in props
        assert "ownership" in props

    c_source = damer_compile.compile_bpf_c(plan)
    assert '#include "damer/bpftime_frontend.h"' in c_source
    assert "struct damer_edge_event" in c_source
    assert "damer_emit_edge" in c_source

    manifest = damer_compile.compile_manifest(plan)
    assert manifest["kind"] == "damer.bpftime.middleware.manifest"
    assert manifest["switchlet"] == plan["switchlet"]
    assert "damer_emit_edge" in manifest["helpers"]


def update_coverage(plan: Dict[str, object], coverage: Dict[str, set]) -> None:
    for edge in plan["edges"]:
        coverage["actions"].update(edge["actions"])
        coverage["nodes"].add(edge["source"]["node"])
        coverage["nodes"].add(edge["destination"]["node"])
        coverage["placements"].add(edge["placement"])
        coverage["kinds"].add(edge["kind"])


def save_repro(
    crash_dir: str,
    seed: int,
    index: int,
    kind: str,
    payload: object,
    exc: BaseException,
) -> str:
    os.makedirs(crash_dir, exist_ok=True)
    path = os.path.join(crash_dir, f"damer-fuzz-crash-seed-{seed}-case-{index}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "seed": seed,
                "case": index,
                "kind": kind,
                "payload": payload,
                "exception": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
            f,
            indent=2,
            sort_keys=True,
        )
        f.write("\n")
    return path


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuzz the Damer middleware compiler")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--seed", default="0xDA6E2026")
    parser.add_argument("--invalid-rate", type=float, default=0.10)
    parser.add_argument("--crash-dir", default=os.path.join("out", "damer-fuzzer-crashes"))
    parser.add_argument("--max-transforms", type=int, default=8)
    parser.add_argument("--max-rdma-ops", type=int, default=16)
    parser.add_argument("--default-ttl", type=int, default=4)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.iterations <= 0:
        raise SystemExit("--iterations must be positive")
    if not 0.0 <= args.invalid_rate <= 1.0:
        raise SystemExit("--invalid-rate must be in [0.0, 1.0]")

    seed = parse_seed(args.seed)
    rng = random.Random(seed)
    options = damer_compile.CompileOptions(
        max_transforms=args.max_transforms,
        max_rdma_ops=args.max_rdma_ops,
        default_ttl=args.default_ttl,
        strict=args.strict,
    )
    coverage = {
        "actions": set(),
        "nodes": set(),
        "placements": set(),
        "kinds": set(),
    }
    timings_us: List[float] = []
    accepted = 0
    rejected = 0
    unexpected = 0
    multi_edge_cases = 0
    first_crash = None

    for index in range(args.iterations):
        kind, payload = make_valid_case(index, rng)
        expected_valid = index < 64 or rng.random() >= args.invalid_rate
        if not expected_valid:
            kind, payload = mutate_invalid(kind, payload, rng)

        start = time.perf_counter_ns()
        try:
            plan = compile_payload(kind, payload, options)
            timings_us.append((time.perf_counter_ns() - start) / 1000.0)
            assert_plan_invariants(plan)
            update_coverage(plan, coverage)
            if len(plan["edges"]) > 1:
                multi_edge_cases += 1
            if expected_valid and not plan["verifier"]["ok"]:
                raise AssertionError(
                    "valid case was rejected by verifier: "
                    + json.dumps(plan["verifier"], sort_keys=True)
                )
            if plan["verifier"]["ok"]:
                accepted += 1
            else:
                rejected += 1
        except damer_compile.CompileError:
            timings_us.append((time.perf_counter_ns() - start) / 1000.0)
            if expected_valid:
                unexpected += 1
                first_crash = save_repro(
                    args.crash_dir,
                    seed,
                    index,
                    kind,
                    payload,
                    sys.exc_info()[1],
                )
                break
            rejected += 1
        except (AssertionError, KeyError, TypeError, ValueError, AttributeError) as exc:
            timings_us.append((time.perf_counter_ns() - start) / 1000.0)
            unexpected += 1
            first_crash = save_repro(args.crash_dir, seed, index, kind, payload, exc)
            break

    ok = unexpected == 0
    median_us = statistics.median(timings_us)
    p95_us = percentile(timings_us, 0.95)
    print(
        "FUZZ iterations={iterations} ok={ok} seed={seed} accepted={accepted} rejected={rejected} unexpected={unexpected}".format(
            iterations=args.iterations,
            ok=str(ok).lower(),
            seed=seed,
            accepted=accepted,
            rejected=rejected,
            unexpected=unexpected,
        )
    )
    print(
        "FUZZ_COVERAGE actions={actions} nodes={nodes} placements={placements} kinds={kinds} multi_edge_cases={multi_edge_cases}".format(
            actions=len(coverage["actions"]),
            nodes=len(coverage["nodes"]),
            placements=len(coverage["placements"]),
            kinds=len(coverage["kinds"]),
            multi_edge_cases=multi_edge_cases,
        )
    )
    print(
        "FUZZ_TIMING compile_median_us={median:.3f} compile_p95_us={p95:.3f}".format(
            median=median_us,
            p95=p95_us,
        )
    )
    if first_crash:
        print(f"FUZZ_CRASH repro={first_crash}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
