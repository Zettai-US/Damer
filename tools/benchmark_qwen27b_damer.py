#!/usr/bin/env python3
"""Benchmark Damer compilation for the Qwen 27B DPU movement workload."""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKLOAD = os.path.join(ROOT, "examples", "qwen27b", "qwen27b_workload.json")
DEFAULT_OUT = os.path.join(ROOT, "out", "qwen27b-damer-benchmark")


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
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_ms": (end - start) / 1_000_000.0,
    }


def load_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def summarize(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"min": 0.0, "median": 0.0, "mean": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def count_bpf_instructions(objdump_output: str) -> int:
    return sum(1 for line in objdump_output.splitlines() if re.match(r"^\s*[0-9]+:", line))


def file_size(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


def phase_kind(plan_path: str) -> str:
    plan = load_json(plan_path)
    if not plan.get("edges"):
        return "unknown"
    return plan["edges"][0]["kind"]


def phase_placement(plan_path: str) -> str:
    plan = load_json(plan_path)
    if not plan.get("edges"):
        return "unknown"
    return plan["edges"][0]["placement"]


def bench_one(
    event: Dict[str, object],
    run_dir: str,
    clang: str,
    llvm_objdump: Optional[str],
) -> Dict[str, object]:
    switchlet = str(event["switchlet"])
    event_path = os.path.join(run_dir, "events", f"{switchlet}.event.json")
    artifact_dir = os.path.join(run_dir, "artifacts", switchlet)
    write_json(event_path, event)

    compile_cmd = [
        os.path.join(ROOT, "tools", "damer_compile.py"),
        event_path,
        "--emit",
        "all",
        "--output-dir",
        artifact_dir,
        "--max-rdma-ops",
        "16",
    ]
    compile_result = run_command(compile_cmd)
    record: Dict[str, object] = {
        "switchlet": switchlet,
        "compile_ms": compile_result["duration_ms"],
        "compile_rc": compile_result["returncode"],
        "clang_ms": 0.0,
        "clang_rc": None,
        "total_ms": compile_result["duration_ms"],
        "object_bytes": 0,
        "bpf_instructions": 0,
        "ok": compile_result["returncode"] == 0,
    }
    if compile_result["returncode"] != 0:
        record["error"] = compile_result["stderr"] or compile_result["stdout"]
        return record

    plan_path = os.path.join(artifact_dir, f"{switchlet}.plan.json")
    manifest_path = os.path.join(artifact_dir, f"{switchlet}.bpftime.json")
    bpf_c_path = os.path.join(artifact_dir, f"{switchlet}.bpf.c")
    bpf_o_path = os.path.join(artifact_dir, f"{switchlet}.bpf.o")

    clang_cmd = [
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
    clang_result = run_command(clang_cmd)
    record["clang_ms"] = clang_result["duration_ms"]
    record["clang_rc"] = clang_result["returncode"]
    record["total_ms"] = record["compile_ms"] + record["clang_ms"]
    record["ok"] = bool(record["ok"]) and clang_result["returncode"] == 0
    if clang_result["returncode"] != 0:
        record["error"] = clang_result["stderr"] or clang_result["stdout"]
        return record

    record.update(
        {
            "kind": phase_kind(plan_path),
            "placement": phase_placement(plan_path),
            "plan_bytes": file_size(plan_path),
            "manifest_bytes": file_size(manifest_path),
            "bpf_c_bytes": file_size(bpf_c_path),
            "object_bytes": file_size(bpf_o_path),
        }
    )

    if llvm_objdump:
        objdump = run_command([llvm_objdump, "-d", bpf_o_path])
        if objdump["returncode"] == 0:
            record["bpf_instructions"] = count_bpf_instructions(objdump["stdout"])

    return record


def aggregate(records: List[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(str(record["switchlet"]), []).append(record)

    summary = {}
    for switchlet, rows in sorted(grouped.items()):
        ok_rows = [row for row in rows if row.get("ok")]
        summary[switchlet] = {
            "runs": len(rows),
            "ok_runs": len(ok_rows),
            "kind": ok_rows[0].get("kind", "unknown") if ok_rows else "unknown",
            "placement": ok_rows[0].get("placement", "unknown") if ok_rows else "unknown",
            "compile_ms": summarize([float(row["compile_ms"]) for row in ok_rows]),
            "clang_ms": summarize([float(row["clang_ms"]) for row in ok_rows]),
            "total_ms": summarize([float(row["total_ms"]) for row in ok_rows]),
            "object_bytes": int(statistics.median([int(row["object_bytes"]) for row in ok_rows]))
            if ok_rows
            else 0,
            "bpf_instructions": int(statistics.median([int(row["bpf_instructions"]) for row in ok_rows]))
            if ok_rows
            else 0,
        }
    return summary


def write_markdown(path: str, report: Dict[str, object]) -> None:
    lines = [
        "# Qwen27B Damer Benchmark",
        "",
        f"- repeats: `{report['config']['repeat']}`",
        f"- warmup: `{report['config']['warmup']}`",
        f"- ok: `{report['ok']}`",
        "",
        "| switchlet | kind | placement | compile median ms | clang median ms | total p95 ms | object bytes | BPF insns |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for switchlet, row in report["summary"].items():
        lines.append(
            "| {switchlet} | {kind} | {placement} | {compile:.3f} | {clang:.3f} | {total:.3f} | {obj} | {insn} |".format(
                switchlet=switchlet,
                kind=row["kind"],
                placement=row["placement"],
                compile=row["compile_ms"]["median"],
                clang=row["clang_ms"]["median"],
                total=row["total_ms"]["p95"],
                obj=row["object_bytes"],
                insn=row["bpf_instructions"],
            )
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")


def run_benchmark(args: argparse.Namespace) -> Dict[str, object]:
    workload = load_json(args.workload)
    out_dir = os.path.abspath(args.out_dir)
    if args.clean and os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    records: List[Dict[str, object]] = []
    total_runs = args.warmup + args.repeat
    for run_index in range(total_runs):
        run_dir = os.path.join(out_dir, "warmup" if run_index < args.warmup else "runs", f"run_{run_index:03d}")
        for event in workload["events"]:
            record = bench_one(event, run_dir, args.clang, args.llvm_objdump)
            if run_index >= args.warmup:
                record["run"] = run_index - args.warmup
                records.append(record)

    report = {
        "ok": all(record.get("ok") for record in records),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "workload": os.path.abspath(args.workload),
            "repeat": args.repeat,
            "warmup": args.warmup,
            "clang": args.clang,
            "llvm_objdump": args.llvm_objdump,
        },
        "workload": {
            "model": workload.get("model", "qwen27b"),
            "events": len(workload["events"]),
        },
        "records": records,
        "summary": aggregate(records),
    }

    report_path = os.path.join(out_dir, "qwen27b_damer_benchmark.json")
    markdown_path = os.path.join(out_dir, "qwen27b_damer_benchmark.md")
    write_json(report_path, report)
    write_markdown(markdown_path, report)
    report["report_path"] = report_path
    report["markdown_path"] = markdown_path
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Damer Qwen27B policy compilation")
    parser.add_argument("--workload", default=DEFAULT_WORKLOAD)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--llvm-objdump", default="llvm-objdump")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_benchmark(args)
    except Exception as exc:
        sys.stderr.write(f"qwen27b-damer-benchmark: error: {exc}\n")
        return 1

    print(json.dumps(
        {
            "ok": report["ok"],
            "events": report["workload"]["events"],
            "repeat": report["config"]["repeat"],
            "report": report["report_path"],
            "markdown": report["markdown_path"],
        },
        indent=2,
        sort_keys=True,
    ))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
