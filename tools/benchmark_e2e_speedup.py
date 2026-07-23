#!/usr/bin/env python3
"""Run multi-workload Damer E2E data-movement speedup benchmarks.

The benchmark has two parts:
1. Real artifact E2E: compile each workload event into a Damer plan, bpftime C,
   and a BPF object, optionally recompiling those BPF programs on a remote DPU.
2. Modeled data-movement E2E: compare a baseline staged movement pipeline with
   Damer fused/offloaded placement using an explicit cost model recorded in the
   report.
"""

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import damer_compile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "out", "e2e-speedup-benchmark")
DEFAULT_WORKLOADS = [
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_dpu_decode_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_long_context_cxl_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_pipeline_parallel_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_lora_adapter_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_scaleout_2gpu_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_scaleout_4gpu_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_scaleout_8gpu_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_scaleout_16gpu_workload.json"),
    os.path.join(ROOT, "examples", "qwen27b", "qwen27b_scaleout_32gpu_workload.json"),
    os.path.join(ROOT, "examples", "workloads", "moe_alltoall_workload.json"),
    os.path.join(ROOT, "examples", "workloads", "prefill_decode_colocation_workload.json"),
    os.path.join(ROOT, "examples", "workloads", "remote_kv_cache_workload.json"),
    os.path.join(ROOT, "examples", "workloads", "training_checkpoint_workload.json"),
]

ASSUMPTIONS = {
    "kind": "modeled_data_movement_e2e_speedup",
    "units": "microseconds",
    "baseline": {
        "description": "staged host/NIC-visible movement with separate transform/control steps",
        "control_overhead_us": 18.0,
        "transform_bandwidth_gbps": 24.0,
        "extra_stage_bytes_per_transform": 0.5,
    },
    "damer": {
        "description": "Damer fused movement with placement from the verifier plan and bpftime offload control",
        "control_overhead_us": 4.0,
        "fragment_control_weight": 0.25,
        "replica_control_weight": 0.5,
    },
    "link_bandwidth_gbps": {
        "host_memory<->gpu_memory": 48.0,
        "host_memory<->cxl_memory_device": 32.0,
        "cxl_memory_device<->gpu_memory": 48.0,
        "gpu_memory<->gpu_memory": 100.0,
        "gpu_memory<->accelerator": 140.0,
        "gpu_memory<->switch_compute_engine": 100.0,
        "cxl_memory_device<->switch_compute_engine": 80.0,
        "default": 32.0,
    },
    "placement_transform_bandwidth_gbps": {
        "host_memory": 24.0,
        "cxl_memory_device": 60.0,
        "gpu_memory": 900.0,
        "accelerator": 180.0,
        "switch_compute_engine": 120.0,
        "unknown": 24.0,
    },
    "default_output_ratios": {
        "quantize": 0.5,
        "compress": 0.4,
        "filter": 0.5,
        "reduce": 0.25,
        "checksum": 1.0,
        "scatter_gather": 1.0,
        "replicate": 1.0,
        "persist": 1.0,
    },
    "contention_queue_us": {
        "priority_8_to_10": {"baseline": 40.0, "damer": 8.0},
        "priority_4_to_7": {"baseline": 20.0, "damer": 10.0},
        "priority_0_to_3": {"baseline": 8.0, "damer": 12.0},
    },
}


def run_command(argv: Sequence[str], cwd: str = ROOT) -> Dict[str, object]:
    start = time.perf_counter_ns()
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    end = time.perf_counter_ns()
    return {
        "cmd": list(argv),
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "duration_ms": (end - start) / 1_000_000.0,
    }


def write_json(path: str, data: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def load_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def sanitize(value: str) -> str:
    return damer_compile.sanitize_c_identifier(value)


def as_float(value: object, fallback: float = 0.0) -> float:
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return fallback


def as_int(value: object, fallback: int = 0) -> int:
    return int(as_float(value, float(fallback)))


def workload_paths(args: argparse.Namespace) -> List[str]:
    paths = list(args.workload or [])
    if args.workload_dir:
        for name in sorted(os.listdir(args.workload_dir)):
            if name.endswith(".json"):
                paths.append(os.path.join(args.workload_dir, name))
    if not paths:
        paths.extend(DEFAULT_WORKLOADS)
    return [os.path.abspath(path) for path in paths]


def gbps_to_bytes_per_us(gbps: float) -> float:
    return gbps * 1000.0


def link_key(source: str, destination: str) -> str:
    return "<->".join(sorted([source, destination]))


def link_bandwidth_gbps(source: str, destination: str) -> float:
    table = ASSUMPTIONS["link_bandwidth_gbps"]
    return float(table.get(link_key(source, destination), table["default"]))


def transfer_us(byte_count: float, source: str, destination: str) -> float:
    if byte_count <= 0:
        return 0.0
    return byte_count / gbps_to_bytes_per_us(link_bandwidth_gbps(source, destination))


def placement_transform_bw_gbps(placement: str) -> float:
    table = ASSUMPTIONS["placement_transform_bandwidth_gbps"]
    return float(table.get(placement, table["unknown"]))


def transform_output_ratio(actions: Sequence[str], attrs: Dict[str, object]) -> float:
    ratio = 1.0
    normalized = [damer_compile.normalize_name(action) for action in actions if action != "move"]
    if "quantize" in normalized:
        ratio *= as_float(attrs.get("quantization_ratio"), ASSUMPTIONS["default_output_ratios"]["quantize"])
    if "compress" in normalized:
        ratio *= as_float(attrs.get("compression_ratio"), ASSUMPTIONS["default_output_ratios"]["compress"])
    if "filter" in normalized:
        ratio *= as_float(attrs.get("filter_selectivity"), ASSUMPTIONS["default_output_ratios"]["filter"])
    if "reduce" in normalized:
        ratio *= as_float(attrs.get("reduction_ratio"), ASSUMPTIONS["default_output_ratios"]["reduce"])
    return min(max(ratio, 0.01), 8.0)


def queue_us(priority: int) -> Tuple[float, float]:
    table = ASSUMPTIONS["contention_queue_us"]
    if priority >= 8:
        row = table["priority_8_to_10"]
    elif priority >= 4:
        row = table["priority_4_to_7"]
    else:
        row = table["priority_0_to_3"]
    return float(row["baseline"]), float(row["damer"])


def modeled_event_latency(event: Dict[str, object], plan: Dict[str, object]) -> Dict[str, object]:
    edge = plan["edges"][0]
    attrs = dict(event.get("edge") or {})
    properties = edge["properties"]
    actions = edge["actions"]
    transforms = [action for action in actions if action != "move"]
    transform_count = len(transforms)
    raw_bytes = as_float(attrs.get("bytes", properties.get("bytes")), 0.0)
    source = edge["source"]["node"]
    destination = edge["destination"]["node"]
    placement = str(edge["placement"])
    priority = as_int(attrs.get("priority", properties.get("priority")), 0)
    fragments = as_int(attrs.get("fragments"), 1) if "scatter_gather" in transforms else 1
    fanout = as_int(attrs.get("fanout"), 1) if "replicate" in transforms else 1
    if "replicate" in transforms:
        fanout = max(fanout, 2)
    if "scatter_gather" in transforms:
        fragments = max(fragments, 2)

    output_ratio = transform_output_ratio(actions, attrs)
    output_bytes = raw_bytes * output_ratio
    baseline_queue_us, damer_queue_us = queue_us(priority)

    baseline_transfer_bytes = raw_bytes * fanout
    baseline_stage_bytes = raw_bytes * transform_count * float(
        ASSUMPTIONS["baseline"]["extra_stage_bytes_per_transform"]
    )
    baseline_transfer = transfer_us(baseline_transfer_bytes + baseline_stage_bytes, source, destination)
    baseline_transform = 0.0
    if transform_count:
        baseline_transform = (
            raw_bytes
            * transform_count
            / gbps_to_bytes_per_us(float(ASSUMPTIONS["baseline"]["transform_bandwidth_gbps"]))
        )
    baseline_control = float(ASSUMPTIONS["baseline"]["control_overhead_us"]) * (
        1 + transform_count + fragments + fanout
    )

    damer_transfer = transfer_us(output_bytes * fanout, source, destination)
    damer_transform = 0.0
    if transform_count:
        damer_transform = (
            raw_bytes
            * transform_count
            / gbps_to_bytes_per_us(placement_transform_bw_gbps(placement))
        )
    damer_control = float(ASSUMPTIONS["damer"]["control_overhead_us"]) * (
        1
        + float(ASSUMPTIONS["damer"]["fragment_control_weight"]) * fragments
        + float(ASSUMPTIONS["damer"]["replica_control_weight"]) * fanout
    )

    baseline_total = baseline_transfer + baseline_transform + baseline_control + baseline_queue_us
    damer_total = damer_transfer + damer_transform + damer_control + damer_queue_us
    return {
        "baseline_us": baseline_total,
        "damer_us": damer_total,
        "speedup": baseline_total / max(damer_total, 0.001),
        "raw_bytes": int(raw_bytes),
        "output_bytes": int(output_bytes),
        "output_ratio": output_ratio,
        "fragments": fragments,
        "fanout": fanout,
        "priority": priority,
        "placement": placement,
        "components": {
            "baseline": {
                "transfer_us": baseline_transfer,
                "transform_us": baseline_transform,
                "control_us": baseline_control,
                "queue_us": baseline_queue_us,
            },
            "damer": {
                "transfer_us": damer_transfer,
                "transform_us": damer_transform,
                "control_us": damer_control,
                "queue_us": damer_queue_us,
            },
        },
    }


def compile_bpf_object(
    clang: str,
    bpf_c_path: str,
    bpf_o_path: str,
) -> Dict[str, object]:
    result = run_command(
        [
            clang,
            "-target",
            "bpf",
            "-O2",
            "-g",
            "-I",
            os.path.join(ROOT, "include"),
            "-c",
            bpf_c_path,
            "-o",
            bpf_o_path,
        ]
    )
    result["ok"] = result["returncode"] == 0 and os.path.exists(bpf_o_path)
    return result


def count_bpf_instructions(objdump_output: str) -> int:
    return sum(1 for line in objdump_output.splitlines() if re.match(r"^\s*[0-9]+:", line))


def inspect_bpf_object(path: str, llvm_objdump: Optional[str]) -> Dict[str, object]:
    if not llvm_objdump or not os.path.exists(path):
        return {"available": False}
    result = run_command([llvm_objdump, "-d", path])
    return {
        "available": result["returncode"] == 0,
        "instruction_count": count_bpf_instructions(result["stdout"]) if result["returncode"] == 0 else 0,
        "result": result,
    }


def compile_event(
    workload_name: str,
    event: Dict[str, object],
    output_dir: str,
    clang: str,
    llvm_objdump: Optional[str],
    compile_bpf: bool,
    max_rdma_ops: int,
) -> Dict[str, object]:
    switchlet = damer_compile.parse_json_switchlet(event)
    start = time.perf_counter_ns()
    plan = damer_compile.compile_plan(
        switchlet,
        damer_compile.CompileOptions(max_rdma_ops=max_rdma_ops),
    )
    compile_ms = (time.perf_counter_ns() - start) / 1_000_000.0

    name = sanitize(str(plan["switchlet"]))
    artifact_dir = os.path.join(output_dir, "artifacts", sanitize(workload_name), name)
    damer_compile.emit_all(plan, artifact_dir)
    input_path = os.path.join(artifact_dir, f"{name}.input.json")
    write_json(input_path, event)

    bpf_c_path = os.path.join(artifact_dir, f"{name}.bpf.c")
    bpf_o_path = os.path.join(artifact_dir, f"{name}.bpf.o")
    object_result = None
    object_info = {"available": False}
    if compile_bpf:
        object_result = compile_bpf_object(clang, bpf_c_path, bpf_o_path)
        if object_result["ok"]:
            object_info = inspect_bpf_object(bpf_o_path, llvm_objdump)

    latency = modeled_event_latency(event, plan)
    return {
        "workload": workload_name,
        "switchlet": str(plan["switchlet"]),
        "ok": bool(plan["verifier"]["ok"])
        and (not compile_bpf or bool(object_result and object_result["ok"])),
        "kind": plan["edges"][0]["kind"],
        "placement": plan["edges"][0]["placement"],
        "compile_ms": compile_ms,
        "artifact_dir": artifact_dir,
        "input": input_path,
        "plan": os.path.join(artifact_dir, f"{name}.plan.json"),
        "bpf_c": bpf_c_path,
        "bpftime_manifest": os.path.join(artifact_dir, f"{name}.bpftime.json"),
        "bpf_object": bpf_o_path if object_result and object_result["ok"] else None,
        "verifier": plan["verifier"],
        "object_compile": object_result,
        "object_info": object_info,
        "latency_model": latency,
    }


def compile_workload(
    path: str,
    output_dir: str,
    clang: str,
    llvm_objdump: Optional[str],
    compile_bpf: bool,
    max_rdma_ops: int,
) -> Dict[str, object]:
    workload = load_json(path)
    name = str(workload.get("model") or os.path.splitext(os.path.basename(path))[0])
    events = workload.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{path} must contain events list")
    cases = [
        compile_event(name, event, output_dir, clang, llvm_objdump, compile_bpf, max_rdma_ops)
        for event in events
    ]
    baseline_us = sum(float(case["latency_model"]["baseline_us"]) for case in cases)
    damer_us = sum(float(case["latency_model"]["damer_us"]) for case in cases)
    return {
        "name": name,
        "path": path,
        "description": str(workload.get("description") or ""),
        "events": len(cases),
        "ok": all(case["ok"] for case in cases),
        "baseline_us": baseline_us,
        "damer_us": damer_us,
        "speedup": baseline_us / max(damer_us, 0.001),
        "compile_ms": sum(float(case["compile_ms"]) for case in cases),
        "bpf_objects": sum(1 for case in cases if case.get("bpf_object")),
        "cases": cases,
    }


def make_package(output_dir: str) -> str:
    package_path = os.path.join(os.path.dirname(output_dir), f"{os.path.basename(output_dir)}.tar.gz")
    if os.path.exists(package_path):
        os.remove(package_path)
    with tarfile.open(package_path, "w:gz") as tar:
        tar.add(os.path.join(output_dir, "artifacts"), arcname="artifacts")
        tar.add(os.path.join(ROOT, "include", "damer"), arcname="include/damer")
    return package_path


def remote_compile(
    output_dir: str,
    remote: str,
    remote_dir: str,
    remote_clang: str,
    ssh_options: Sequence[str],
) -> Dict[str, object]:
    package_path = make_package(output_dir)
    ssh_base = ["ssh"] + list(ssh_options) + [remote]
    scp_base = ["scp"] + list(ssh_options)

    mkdir = run_command(ssh_base + [f"rm -rf {remote_dir} && mkdir -p {remote_dir}"])
    if mkdir["returncode"] != 0:
        return {"ok": False, "stage": "mkdir", "package": package_path, "result": mkdir}

    copy = run_command(scp_base + [package_path, f"{remote}:{remote_dir}/e2e-speedup-benchmark.tar.gz"])
    if copy["returncode"] != 0:
        return {"ok": False, "stage": "scp", "package": package_path, "result": copy}

    script = f"""
set -eu
cd {remote_dir}
tar -xzf e2e-speedup-benchmark.tar.gz
count=0
for c in $(find artifacts -name '*.bpf.c' | sort); do
  o="${{c%.c}}.remote.bpf.o"
  {remote_clang} -target bpf -O2 -g -I ./include -c "$c" -o "$o"
  count=$((count + 1))
done
echo "REMOTE_BPF_OBJECTS $count"
find artifacts -name '*.remote.bpf.o' | sort
"""
    result = run_command(ssh_base + [script])
    return {
        "ok": result["returncode"] == 0,
        "stage": "remote-compile",
        "package": package_path,
        "result": result,
    }


def geometric_mean(values: Iterable[float]) -> float:
    vals = [value for value in values if value > 0]
    if not vals:
        return 0.0
    return math.exp(statistics.fmean(math.log(value) for value in vals))


def write_markdown(path: str, report: Dict[str, object]) -> None:
    lines = [
        "# Damer E2E Speedup Benchmark",
        "",
        f"- ok: `{str(report['ok']).lower()}`",
        f"- speedup kind: `{report['assumptions']['kind']}`",
        f"- workloads: `{len(report['workloads'])}`",
        f"- events: `{report['summary']['events']}`",
        f"- remote ok: `{report['remote']['ok'] if report['remote'] else None}`",
        "",
        "| workload | events | baseline us | Damer us | speedup | BPF objects |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for workload in report["workloads"]:
        lines.append(
            "| {name} | {events} | {baseline:.2f} | {damer:.2f} | {speedup:.2f}x | {objects} |".format(
                name=workload["name"],
                events=workload["events"],
                baseline=workload["baseline_us"],
                damer=workload["damer_us"],
                speedup=workload["speedup"],
                objects=workload["bpf_objects"],
            )
        )
    lines.extend(
        [
            "",
            "## Event Detail",
            "",
            "| workload | switchlet | kind | placement | baseline us | Damer us | speedup | output ratio |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for workload in report["workloads"]:
        for case in workload["cases"]:
            latency = case["latency_model"]
            lines.append(
                "| {workload} | {switchlet} | {kind} | {placement} | {baseline:.2f} | {damer:.2f} | {speedup:.2f}x | {ratio:.3f} |".format(
                    workload=workload["name"],
                    switchlet=case["switchlet"],
                    kind=case["kind"],
                    placement=case["placement"],
                    baseline=latency["baseline_us"],
                    damer=latency["damer_us"],
                    speedup=latency["speedup"],
                    ratio=latency["output_ratio"],
                )
            )
    lines.extend(
        [
            "",
            "## Assumptions",
            "",
            "This is a modeled data-movement speedup, not an application token-latency measurement.",
            "The same workload artifacts are still compiled end to end into bpftime-facing BPF objects.",
        ]
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")


def run_benchmark(args: argparse.Namespace) -> Dict[str, object]:
    output_dir = os.path.abspath(args.out_dir)
    if args.clean and os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    workloads = [
        compile_workload(
            path,
            output_dir,
            args.clang,
            args.llvm_objdump,
            not args.no_bpf_object,
            args.max_rdma_ops,
        )
        for path in workload_paths(args)
    ]
    remote = None
    if args.remote:
        remote = remote_compile(
            output_dir,
            args.remote,
            args.remote_dir,
            args.remote_clang,
            args.ssh_option or [],
        )

    summary = {
        "workloads": len(workloads),
        "events": sum(int(workload["events"]) for workload in workloads),
        "bpf_objects": sum(int(workload["bpf_objects"]) for workload in workloads),
        "baseline_us": sum(float(workload["baseline_us"]) for workload in workloads),
        "damer_us": sum(float(workload["damer_us"]) for workload in workloads),
        "geomean_speedup": geometric_mean(float(workload["speedup"]) for workload in workloads),
    }
    summary["aggregate_speedup"] = summary["baseline_us"] / max(summary["damer_us"], 0.001)

    report = {
        "ok": all(workload["ok"] for workload in workloads) and (remote is None or bool(remote["ok"])),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "damer.e2e.speedup_benchmark",
        "config": {
            "workloads": workload_paths(args),
            "clang": args.clang,
            "llvm_objdump": args.llvm_objdump,
            "remote": args.remote,
            "remote_dir": args.remote_dir,
            "remote_clang": args.remote_clang,
            "max_rdma_ops": args.max_rdma_ops,
        },
        "assumptions": ASSUMPTIONS,
        "summary": summary,
        "workloads": workloads,
        "remote": remote,
    }
    report_path = os.path.join(output_dir, "e2e_speedup_report.json")
    markdown_path = os.path.join(output_dir, "e2e_speedup_report.md")
    write_json(report_path, report)
    write_markdown(markdown_path, report)
    report["report_path"] = report_path
    report["markdown_path"] = markdown_path
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Damer multi-workload E2E speedup")
    parser.add_argument("--workload", action="append", help="workload JSON; repeat to include multiple workloads")
    parser.add_argument("--workload-dir", help="directory of workload JSON files")
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--llvm-objdump", default="llvm-objdump")
    parser.add_argument("--no-bpf-object", action="store_true")
    parser.add_argument("--remote", help="optional DPU SSH target for remote BPF compile")
    parser.add_argument("--remote-dir", default="/tmp/damer-e2e-speedup")
    parser.add_argument("--remote-clang", default="clang")
    parser.add_argument("--max-rdma-ops", type=int, default=128)
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="extra option passed to ssh/scp; repeat for each argument",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_benchmark(args)
    except Exception as exc:
        sys.stderr.write(f"e2e-speedup: error: {exc}\n")
        return 1

    print(
        "E2E_SPEEDUP ok={ok} workloads={workloads} events={events} bpf_objects={objects} "
        "aggregate_speedup={aggregate:.3f} geomean_speedup={geomean:.3f} remote_ok={remote_ok}".format(
            ok=str(report["ok"]).lower(),
            workloads=report["summary"]["workloads"],
            events=report["summary"]["events"],
            objects=report["summary"]["bpf_objects"],
            aggregate=report["summary"]["aggregate_speedup"],
            geomean=report["summary"]["geomean_speedup"],
            remote_ok=None if report["remote"] is None else str(report["remote"]["ok"]).lower(),
        )
    )
    print(f"E2E_SPEEDUP_REPORT path={report['report_path']}")
    print(f"E2E_SPEEDUP_MARKDOWN path={report['markdown_path']}")
    for workload in report["workloads"]:
        print(
            "E2E_WORKLOAD name={name} events={events} speedup={speedup:.3f} baseline_us={baseline:.3f} damer_us={damer:.3f}".format(
                name=workload["name"],
                events=workload["events"],
                speedup=workload["speedup"],
                baseline=workload["baseline_us"],
                damer=workload["damer_us"],
            )
        )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
