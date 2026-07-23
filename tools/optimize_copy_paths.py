#!/usr/bin/env python3
"""Analyze DPU/GPU copy-path opportunities from a Damer E2E report.

This is a post-processing optimizer over compiled Damer workload artifacts. It
keeps the original E2E benchmark's Damer latency model as the current path, then
evaluates candidate copy paths that are intended to be observable by dputime on
both the GPU host and BlueField DPU.
"""

import argparse
import collections
import json
import math
import os
import textwrap
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import benchmark_e2e_speedup as bench


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPORT = os.path.join(ROOT, "out", "e2e-speedup-benchmark", "e2e_speedup_report.json")
DEFAULT_OUT = os.path.join(ROOT, "out", "copy-path-optimization")
CRITICAL_PRIORITY = 8

GPU_PRETRANSFORM_ACTIONS = {"quantize", "compress", "checksum", "filter", "reduce"}

ASSUMPTIONS = {
    "kind": "modeled_dpu_gpu_copy_path_optimization",
    "units": "microseconds",
    "description": (
        "What-if optimization over an existing Damer E2E report. The current "
        "Damer path is preserved as a candidate and only replaced when a "
        "DPU/GPU copy-path candidate has lower modeled latency."
    ),
    "candidate_paths": {
        "current_damer": "original Damer placement and cost model from the E2E benchmark",
        "dpu_inline": (
            "BlueField/DPU inline copy path using GPUDirect-facing movement, "
            "lower control overhead, and DPU-side transform placement"
        ),
        "gpu_pretransform_direct": (
            "GPU source performs quantize/compress/filter/reduce/checksum before "
            "the DPU or NIC copies fewer bytes"
        ),
        "gpu_peer_direct": (
            "direct GPU-memory to GPU-memory copy/RDMA path for peer or "
            "scatter-gather movement"
        ),
    },
    "optimized_link_bandwidth_gbps": {
        "host_memory<->gpu_memory": 56.0,
        "cxl_memory_device<->gpu_memory": 64.0,
        "gpu_memory<->gpu_memory": 180.0,
        "gpu_memory<->accelerator": 180.0,
        "gpu_memory<->switch_compute_engine": 140.0,
        "cxl_memory_device<->switch_compute_engine": 120.0,
        "default": 32.0,
    },
    "dpu_inline": {
        "control_overhead_us": 3.0,
        "fragment_control_weight": 0.20,
        "replica_control_weight": 0.40,
        "transform_bandwidth_gbps": 160.0,
    },
    "gpu_pretransform_direct": {
        "control_overhead_us": 3.0,
        "fragment_control_weight": 0.15,
        "replica_control_weight": 0.25,
        "transform_bandwidth_gbps": 900.0,
    },
    "contention_queue_us": {
        "priority_8_to_10": 6.0,
        "priority_4_to_7": 8.0,
        "priority_0_to_3": 14.0,
    },
}


def load_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def resolve_path(path: object, base_dir: str) -> Optional[str]:
    if not path:
        return None
    text = str(path)
    if os.path.isabs(text):
        return text
    return os.path.abspath(os.path.join(base_dir, text))


def gbps_to_bytes_per_us(gbps: float) -> float:
    return gbps * 1000.0


def optimized_link_bandwidth_gbps(source: str, destination: str) -> float:
    table = ASSUMPTIONS["optimized_link_bandwidth_gbps"]
    current = bench.link_bandwidth_gbps(source, destination)
    optimized = float(table.get(bench.link_key(source, destination), table["default"]))
    return max(current, optimized)


def transfer_us(byte_count: float, source: str, destination: str) -> float:
    if byte_count <= 0:
        return 0.0
    return byte_count / gbps_to_bytes_per_us(optimized_link_bandwidth_gbps(source, destination))


def optimized_queue_us(priority: int) -> float:
    table = ASSUMPTIONS["contention_queue_us"]
    if priority >= 8:
        return float(table["priority_8_to_10"])
    if priority >= 4:
        return float(table["priority_4_to_7"])
    return float(table["priority_0_to_3"])


def normalized_transforms(actions: Sequence[object]) -> List[str]:
    return [
        bench.damer_compile.normalize_name(str(action))
        for action in actions
        if bench.damer_compile.normalize_name(str(action)) != "move"
    ]


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / max(denominator, 0.001)


def event_context(case: Dict[str, object], report_dir: str) -> Dict[str, object]:
    plan_path = resolve_path(case.get("plan"), report_dir)
    input_path = resolve_path(case.get("input"), report_dir)
    plan = load_json(plan_path) if plan_path and os.path.exists(plan_path) else {}
    event = load_json(input_path) if input_path and os.path.exists(input_path) else {}

    edge = {}
    if plan.get("edges"):
        edge = plan["edges"][0]
    attrs = dict(event.get("edge") or edge.get("properties") or {})
    properties = edge.get("properties") or {}

    actions = edge.get("actions") or event.get("actions") or ["move"]
    transforms = normalized_transforms(actions)
    source = str(
        ((edge.get("source") or {}).get("node"))
        or ((event.get("source") or {}).get("node"))
        or "unknown"
    )
    destination = str(
        ((edge.get("destination") or {}).get("node"))
        or ((event.get("destination") or {}).get("node"))
        or "unknown"
    )
    raw_bytes = bench.as_float(attrs.get("bytes", properties.get("bytes")), 0.0)
    output_ratio = bench.transform_output_ratio(actions, attrs)
    output_bytes = raw_bytes * output_ratio
    priority = bench.as_int(attrs.get("priority", properties.get("priority")), 0)
    fragments = bench.as_int(attrs.get("fragments"), 1) if "scatter_gather" in transforms else 1
    fanout = bench.as_int(attrs.get("fanout"), 1) if "replicate" in transforms else 1
    if "scatter_gather" in transforms:
        fragments = max(fragments, 2)
    if "replicate" in transforms:
        fanout = max(fanout, 2)

    return {
        "plan": plan,
        "event": event,
        "attrs": attrs,
        "actions": [str(action) for action in actions],
        "transforms": transforms,
        "source": source,
        "destination": destination,
        "raw_bytes": raw_bytes,
        "output_ratio": output_ratio,
        "output_bytes": output_bytes,
        "priority": priority,
        "fragments": fragments,
        "fanout": fanout,
    }


def control_us(path_name: str, transform_count: int, fragments: int, fanout: int) -> float:
    cfg = ASSUMPTIONS[path_name]
    return float(cfg["control_overhead_us"]) * (
        1.0
        + 0.10 * transform_count
        + float(cfg["fragment_control_weight"]) * fragments
        + float(cfg["replica_control_weight"]) * fanout
    )


def candidate_current(case: Dict[str, object]) -> Dict[str, object]:
    latency = case["latency_model"]
    components = dict((latency.get("components") or {}).get("damer") or {})
    return {
        "path": "current_damer",
        "total_us": float(latency["damer_us"]),
        "eligible": True,
        "reason": "existing Damer placement",
        "components": components,
    }


def candidate_dpu_inline(ctx: Dict[str, object]) -> Optional[Dict[str, object]]:
    source = str(ctx["source"])
    destination = str(ctx["destination"])
    if source == "unknown" or destination == "unknown":
        return None
    transform_count = len(ctx["transforms"])
    transfer = transfer_us(float(ctx["output_bytes"]) * int(ctx["fanout"]), source, destination)
    transform = 0.0
    if transform_count:
        transform = (
            float(ctx["raw_bytes"])
            * transform_count
            / gbps_to_bytes_per_us(float(ASSUMPTIONS["dpu_inline"]["transform_bandwidth_gbps"]))
        )
    control = control_us("dpu_inline", transform_count, int(ctx["fragments"]), int(ctx["fanout"]))
    queue = optimized_queue_us(int(ctx["priority"]))
    return {
        "path": "dpu_inline",
        "total_us": transfer + transform + control + queue,
        "eligible": True,
        "reason": "inline DPU/GPUDirect copy path",
        "components": {
            "transfer_us": transfer,
            "transform_us": transform,
            "control_us": control,
            "queue_us": queue,
        },
    }


def candidate_gpu_pretransform(ctx: Dict[str, object]) -> Optional[Dict[str, object]]:
    transforms = set(ctx["transforms"])
    if str(ctx["source"]) != "gpu_memory" or not (transforms & GPU_PRETRANSFORM_ACTIONS):
        return None
    transform_count = len(ctx["transforms"])
    source = str(ctx["source"])
    destination = str(ctx["destination"])
    transfer = transfer_us(float(ctx["output_bytes"]) * int(ctx["fanout"]), source, destination)
    transform = (
        float(ctx["raw_bytes"])
        * transform_count
        / gbps_to_bytes_per_us(float(ASSUMPTIONS["gpu_pretransform_direct"]["transform_bandwidth_gbps"]))
    )
    control = control_us(
        "gpu_pretransform_direct",
        transform_count,
        int(ctx["fragments"]),
        int(ctx["fanout"]),
    )
    queue = optimized_queue_us(int(ctx["priority"]))
    return {
        "path": "gpu_pretransform_direct",
        "total_us": transfer + transform + control + queue,
        "eligible": True,
        "reason": "run shrinking transform on GPU before DPU/NIC copy",
        "components": {
            "transfer_us": transfer,
            "transform_us": transform,
            "control_us": control,
            "queue_us": queue,
        },
    }


def candidate_gpu_peer(ctx: Dict[str, object]) -> Optional[Dict[str, object]]:
    if str(ctx["source"]) != "gpu_memory" or str(ctx["destination"]) != "gpu_memory":
        return None
    transform_count = len(ctx["transforms"])
    source = str(ctx["source"])
    destination = str(ctx["destination"])
    transfer = transfer_us(float(ctx["output_bytes"]) * int(ctx["fanout"]), source, destination)
    transform = 0.0
    if transform_count:
        transform = (
            float(ctx["raw_bytes"])
            * transform_count
            / gbps_to_bytes_per_us(float(ASSUMPTIONS["gpu_pretransform_direct"]["transform_bandwidth_gbps"]))
        )
    control = control_us(
        "gpu_pretransform_direct",
        transform_count,
        int(ctx["fragments"]),
        int(ctx["fanout"]),
    )
    queue = optimized_queue_us(int(ctx["priority"]))
    return {
        "path": "gpu_peer_direct",
        "total_us": transfer + transform + control + queue,
        "eligible": True,
        "reason": "direct GPU peer/RDMA movement",
        "components": {
            "transfer_us": transfer,
            "transform_us": transform,
            "control_us": control,
            "queue_us": queue,
        },
    }


def evaluate_case(
    workload_name: str,
    case: Dict[str, object],
    report_dir: str,
) -> Dict[str, object]:
    ctx = event_context(case, report_dir)
    candidates = [candidate_current(case)]
    for candidate in (
        candidate_dpu_inline(ctx),
        candidate_gpu_pretransform(ctx),
        candidate_gpu_peer(ctx),
    ):
        if candidate is not None:
            candidates.append(candidate)

    best = min(candidates, key=lambda item: float(item["total_us"]))
    current_us = float(case["latency_model"]["damer_us"])
    optimized_us = float(best["total_us"])
    baseline_us = float(case["latency_model"]["baseline_us"])
    return {
        "workload": workload_name,
        "switchlet": case["switchlet"],
        "kind": case["kind"],
        "source": ctx["source"],
        "destination": ctx["destination"],
        "placement": case["placement"],
        "actions": ctx["actions"],
        "transforms": ctx["transforms"],
        "priority": ctx["priority"],
        "critical": int(ctx["priority"]) >= CRITICAL_PRIORITY,
        "raw_bytes": int(ctx["raw_bytes"]),
        "output_bytes": int(ctx["output_bytes"]),
        "output_ratio": float(ctx["output_ratio"]),
        "fragments": int(ctx["fragments"]),
        "fanout": int(ctx["fanout"]),
        "current_damer_us": current_us,
        "optimized_us": optimized_us,
        "baseline_us": baseline_us,
        "savings_us": max(0.0, current_us - optimized_us),
        "savings_pct_of_current": 100.0 * max(0.0, current_us - optimized_us) / max(current_us, 0.001),
        "speedup_over_current": safe_ratio(current_us, optimized_us),
        "baseline_speedup_after_opt": safe_ratio(baseline_us, optimized_us),
        "best_path": best["path"],
        "best_reason": best["reason"],
        "candidates": sorted(candidates, key=lambda item: str(item["path"])),
    }


def summarize_workload(workload: Dict[str, object], events: List[Dict[str, object]]) -> Dict[str, object]:
    current = sum(float(event["current_damer_us"]) for event in events)
    optimized = sum(float(event["optimized_us"]) for event in events)
    baseline = sum(float(event["baseline_us"]) for event in events)
    critical_events = [event for event in events if event["critical"]]
    critical_current = sum(float(event["current_damer_us"]) for event in critical_events)
    critical_optimized = sum(float(event["optimized_us"]) for event in critical_events)
    counts = collections.Counter(str(event["best_path"]) for event in events)
    return {
        "name": workload["name"],
        "path": workload.get("path"),
        "events": len(events),
        "current_damer_us": current,
        "optimized_us": optimized,
        "baseline_us": baseline,
        "savings_us": max(0.0, current - optimized),
        "improvement_over_current": safe_ratio(current, optimized),
        "baseline_speedup_after_opt": safe_ratio(baseline, optimized),
        "critical_events": len(critical_events),
        "critical_current_damer_us": critical_current,
        "critical_optimized_us": critical_optimized,
        "critical_improvement_over_current": safe_ratio(critical_current, critical_optimized)
        if critical_events
        else 0.0,
        "best_path_counts": dict(sorted(counts.items())),
        "events_detail": events,
    }


def geomean(values: Sequence[float]) -> float:
    vals = [value for value in values if value > 0]
    if not vals:
        return 0.0
    return math.exp(sum(math.log(value) for value in vals) / len(vals))


def build_report(args: argparse.Namespace) -> Dict[str, object]:
    source_report_path = os.path.abspath(args.report)
    source = load_json(source_report_path)
    report_dir = os.path.dirname(source_report_path)

    workloads = []
    all_events = []
    for workload in source["workloads"]:
        events = [
            evaluate_case(str(workload["name"]), case, report_dir)
            for case in workload["cases"]
        ]
        workloads.append(summarize_workload(workload, events))
        all_events.extend(events)

    current = sum(float(event["current_damer_us"]) for event in all_events)
    optimized = sum(float(event["optimized_us"]) for event in all_events)
    baseline = sum(float(event["baseline_us"]) for event in all_events)
    critical_events = [event for event in all_events if event["critical"]]
    critical_current = sum(float(event["current_damer_us"]) for event in critical_events)
    critical_optimized = sum(float(event["optimized_us"]) for event in critical_events)
    path_counts = collections.Counter(str(event["best_path"]) for event in all_events)

    top_savings = sorted(all_events, key=lambda event: float(event["savings_us"]), reverse=True)[
        : int(args.top_events)
    ]
    summary = {
        "workloads": len(workloads),
        "events": len(all_events),
        "current_damer_us": current,
        "optimized_us": optimized,
        "baseline_us": baseline,
        "savings_us": max(0.0, current - optimized),
        "improvement_over_current": safe_ratio(current, optimized),
        "baseline_speedup_after_opt": safe_ratio(baseline, optimized),
        "workload_geomean_improvement": geomean(
            [float(workload["improvement_over_current"]) for workload in workloads]
        ),
        "critical_events": len(critical_events),
        "critical_current_damer_us": critical_current,
        "critical_optimized_us": critical_optimized,
        "critical_improvement_over_current": safe_ratio(critical_current, critical_optimized)
        if critical_events
        else 0.0,
        "best_path_counts": dict(sorted(path_counts.items())),
    }

    return {
        "ok": bool(source.get("ok", False)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "damer.copy_path_optimization",
        "source_report": source_report_path,
        "source_remote_ok": None
        if source.get("remote") is None
        else bool((source.get("remote") or {}).get("ok")),
        "assumptions": ASSUMPTIONS,
        "summary": summary,
        "workloads": workloads,
        "top_savings": top_savings,
    }


def write_markdown(path: str, report: Dict[str, object]) -> None:
    summary = report["summary"]
    lines = [
        "# Damer DPU/GPU Copy-Path Optimization",
        "",
        "This report is a modeled copy-path what-if over already compiled Damer artifacts.",
        "",
        f"- ok: `{str(report['ok']).lower()}`",
        f"- workloads: `{summary['workloads']}`",
        f"- events: `{summary['events']}`",
        f"- current Damer: `{summary['current_damer_us']:.2f} us`",
        f"- optimized copy path: `{summary['optimized_us']:.2f} us`",
        f"- improvement over current Damer: `{summary['improvement_over_current']:.2f}x`",
        f"- baseline to optimized: `{summary['baseline_speedup_after_opt']:.2f}x`",
        f"- critical-priority improvement: `{summary['critical_improvement_over_current']:.2f}x`",
        "",
        "## Workloads",
        "",
        "| workload | events | current Damer us | optimized us | extra speedup | best paths |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for workload in report["workloads"]:
        paths = ", ".join(
            f"{name}:{count}" for name, count in workload["best_path_counts"].items()
        )
        lines.append(
            "| {name} | {events} | {current:.2f} | {optimized:.2f} | {speedup:.2f}x | {paths} |".format(
                name=workload["name"],
                events=workload["events"],
                current=workload["current_damer_us"],
                optimized=workload["optimized_us"],
                speedup=workload["improvement_over_current"],
                paths=paths,
            )
        )

    lines.extend(
        [
            "",
            "## Top Copy-Path Savings",
            "",
            "| workload | switchlet | kind | best path | saved us | current us | optimized us | why |",
            "|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    for event in report["top_savings"]:
        lines.append(
            "| {workload} | {switchlet} | {kind} | {path} | {saved:.2f} | {current:.2f} | {optimized:.2f} | {why} |".format(
                workload=event["workload"],
                switchlet=event["switchlet"],
                kind=event["kind"],
                path=event["best_path"],
                saved=event["savings_us"],
                current=event["current_damer_us"],
                optimized=event["optimized_us"],
                why=event["best_reason"],
            )
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `dpu_inline` mainly helps large `persist`, `replicate`, and `scatter_gather` movements by keeping the copy path on the BlueField/GPUDirect side and reducing staged control work.",
            "- `gpu_pretransform_direct` mainly helps GPU-source `compress`, `reduce`, `filter`, and `quantize` events because bytes are reduced before PCIe/CXL/RDMA movement.",
            "- `gpu_peer_direct` helps GPU-to-GPU movement, but its E2E impact depends on byte volume and whether it sits on a critical priority path.",
            "",
            "This is not a token-latency or NIC counter measurement. It is a reproducible model over real Damer plans, BPF C, and BPF object artifacts.",
        ]
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")


def short_label(value: str) -> str:
    return textwrap.shorten(value.replace("_", " "), width=34, placeholder="...")


def maybe_write_figures(report: Dict[str, object], out_dir: str, formats: Sequence[str]) -> List[str]:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return []

    figure_dir = os.path.join(out_dir, "figures")
    os.makedirs(figure_dir, exist_ok=True)
    top = [event for event in report["top_savings"] if float(event["savings_us"]) > 0.0][:12]
    if not top:
        return []

    labels = [short_label(str(event["switchlet"])) for event in top]
    savings_ms = [float(event["savings_us"]) / 1000.0 for event in top]
    colors = {
        "dpu_inline": "#3B6FB6",
        "gpu_pretransform_direct": "#1B998B",
        "gpu_peer_direct": "#D95D39",
        "current_damer": "#5F6B76",
    }
    bar_colors = [colors.get(str(event["best_path"]), "#5F6B76") for event in top]

    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#AAB2BD",
            "axes.labelcolor": "#1F2933",
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "savefig.bbox": "tight",
            "savefig.facecolor": "white",
        }
    )
    fig, ax = plt.subplots(figsize=(8.0, max(4.8, 0.32 * len(top) + 1.2)))
    y_positions = list(range(len(top)))
    ax.barh(y_positions, savings_ms, color=bar_colors, height=0.72)
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Additional saving over current Damer (ms)")
    ax.set_title("DPU/GPU Copy-Path Optimization Hotspots")
    ax.grid(axis="x", color="#E9EDF2", linewidth=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for y, value in zip(y_positions, savings_ms):
        ax.text(value + max(savings_ms) * 0.015, y, f"{value:.2f}", va="center", fontsize=8)

    outputs = []
    for fmt in formats:
        path = os.path.join(figure_dir, f"copy_path_savings_by_event.{fmt}")
        kwargs = {"format": fmt}
        if fmt == "png":
            kwargs["dpi"] = 240
        fig.savefig(path, **kwargs)
        outputs.append(path)
    plt.close(fig)
    return outputs


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize DPU/GPU copy paths from a Damer E2E report")
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--top-events", type=int, default=12)
    parser.add_argument("--format", action="append", choices=["svg", "png", "pdf"])
    parser.add_argument("--no-figures", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    report = build_report(args)
    report_path = os.path.join(out_dir, "copy_path_optimization.json")
    markdown_path = os.path.join(out_dir, "copy_path_optimization.md")
    write_json(report_path, report)
    write_markdown(markdown_path, report)
    figures = [] if args.no_figures else maybe_write_figures(report, out_dir, args.format or ["svg", "png"])

    summary = report["summary"]
    print(
        "COPY_PATH_OPT ok={ok} workloads={workloads} events={events} "
        "current_damer_us={current:.3f} optimized_us={optimized:.3f} "
        "improvement={improvement:.3f} baseline_speedup={baseline:.3f}".format(
            ok=str(report["ok"]).lower(),
            workloads=summary["workloads"],
            events=summary["events"],
            current=summary["current_damer_us"],
            optimized=summary["optimized_us"],
            improvement=summary["improvement_over_current"],
            baseline=summary["baseline_speedup_after_opt"],
        )
    )
    print(f"COPY_PATH_OPT_REPORT path={report_path}")
    print(f"COPY_PATH_OPT_MARKDOWN path={markdown_path}")
    for path in figures:
        print(f"COPY_PATH_OPT_FIGURE path={path}")
    for workload in report["workloads"]:
        print(
            "COPY_PATH_WORKLOAD name={name} events={events} improvement={improvement:.3f} "
            "optimized_us={optimized:.3f}".format(
                name=workload["name"],
                events=workload["events"],
                improvement=workload["improvement_over_current"],
                optimized=workload["optimized_us"],
            )
        )
    for event in report["top_savings"][:5]:
        print(
            "COPY_PATH_TOP switchlet={switchlet} workload={workload} path={path} "
            "savings_us={savings:.3f}".format(
                switchlet=event["switchlet"],
                workload=event["workload"],
                path=event["best_path"],
                savings=event["savings_us"],
            )
        )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
