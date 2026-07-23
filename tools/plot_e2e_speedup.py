#!/usr/bin/env python3
"""Generate figures from Damer E2E speedup reports."""

import argparse
import json
import os
import textwrap
from typing import Dict, List, Optional, Sequence

import matplotlib.pyplot as plt
from matplotlib.patches import Patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPORT = os.path.join(ROOT, "out", "e2e-speedup-benchmark", "e2e_speedup_report.json")
DEFAULT_OUT = os.path.join(ROOT, "out", "e2e-speedup-benchmark", "figures")

PALETTE = {
    "blue": "#3B6FB6",
    "teal": "#1B998B",
    "gold": "#D99A2B",
    "coral": "#D95D39",
    "purple": "#7E57A0",
    "gray": "#5F6B76",
    "light_gray": "#E9EDF2",
    "dark": "#1F2933",
}

KIND_COLORS = {
    "move": "#5F6B76",
    "move+quantize": "#3B6FB6",
    "move+compress": "#1B998B",
    "move+checksum": "#D99A2B",
    "move+filter": "#6C8E3F",
    "move+reduce": "#D95D39",
    "move+scatter_gather": "#7E57A0",
    "move+replicate": "#B05C8E",
    "move+persist": "#8A6F3D",
    "move+compress+persist": "#2F7D6D",
}


def load_report(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def setup_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#AAB2BD",
            "axes.labelcolor": PALETTE["dark"],
            "axes.titleweight": "bold",
            "axes.titlesize": 13,
            "axes.labelsize": 10,
            "xtick.color": PALETTE["dark"],
            "ytick.color": PALETTE["dark"],
            "font.size": 9,
            "font.family": "DejaVu Sans",
            "legend.frameon": False,
            "savefig.bbox": "tight",
            "savefig.facecolor": "white",
        }
    )


def save_figure(fig: plt.Figure, out_dir: str, name: str, formats: Sequence[str]) -> List[str]:
    paths = []
    for fmt in formats:
        path = os.path.join(out_dir, f"{name}.{fmt}")
        kwargs = {"format": fmt}
        if fmt == "png":
            kwargs["dpi"] = 240
        fig.savefig(path, **kwargs)
        paths.append(path)
    plt.close(fig)
    return paths


def short_workload_label(name: str) -> str:
    labels = {
        "qwen27b": "Qwen\n27B",
        "moe_alltoall": "MoE\nall-to-all",
        "prefill_decode_colocation": "Prefill +\ndecode",
        "remote_kv_cache": "Remote\nKV cache",
        "training_checkpoint": "Training +\ncheckpoint",
    }
    return labels.get(name, "\n".join(textwrap.wrap(name.replace("_", " "), 12)))


def plot_workload_speedup(report: Dict[str, object], out_dir: str, formats: Sequence[str]) -> List[str]:
    workloads = report["workloads"]
    names = [str(w["name"]) for w in workloads]
    speedups = [float(w["speedup"]) for w in workloads]
    colors = [PALETTE["blue"], PALETTE["teal"], PALETTE["gold"], PALETTE["coral"], PALETTE["purple"]]

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    bars = ax.bar(range(len(workloads)), speedups, color=colors[: len(workloads)], width=0.68)
    ax.axhline(1.0, color="#8792A2", linewidth=1.0, linestyle="--")
    ax.set_xticks(range(len(workloads)))
    ax.set_xticklabels([short_workload_label(name) for name in names])
    ax.set_ylabel("Modeled data-movement E2E speedup")
    ax.set_title("Damer Speedup Across GPU Fabric Workloads")
    summary = report["summary"]
    ax.text(
        0.01,
        0.98,
        "aggregate {agg:.2f}x  |  geomean {geo:.2f}x  |  {events} events, {objects} BPF objects".format(
            agg=float(summary["aggregate_speedup"]),
            geo=float(summary["geomean_speedup"]),
            events=int(summary["events"]),
            objects=int(summary["bpf_objects"]),
        ),
        transform=ax.transAxes,
        ha="left",
        va="top",
        color=PALETTE["dark"],
        fontsize=9,
    )
    for bar, value in zip(bars, speedups):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 0.07,
            f"{value:.2f}x",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            color=PALETTE["dark"],
        )
    ax.set_ylim(0, max(speedups) * 1.24)
    ax.grid(axis="y", color=PALETTE["light_gray"], linewidth=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    return save_figure(fig, out_dir, "e2e_speedup_by_workload", formats)


def plot_latency_comparison(report: Dict[str, object], out_dir: str, formats: Sequence[str]) -> List[str]:
    workloads = report["workloads"]
    labels = [short_workload_label(str(w["name"])).replace("\n", " ") for w in workloads]
    baseline_ms = [float(w["baseline_us"]) / 1000.0 for w in workloads]
    damer_ms = [float(w["damer_us"]) / 1000.0 for w in workloads]
    y_positions = list(range(len(workloads)))

    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    ax.barh([y + 0.18 for y in y_positions], baseline_ms, height=0.34, color="#B8C0CC", label="Baseline")
    ax.barh([y - 0.18 for y in y_positions], damer_ms, height=0.34, color=PALETTE["teal"], label="Damer")
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("Modeled movement latency (ms)")
    ax.set_title("Baseline Staging vs Damer Fused Offload")
    for y, base, damer in zip(y_positions, baseline_ms, damer_ms):
        reduction = 100.0 * (1.0 - damer / base)
        ax.text(
            max(base, damer) + max(baseline_ms) * 0.015,
            y,
            f"-{reduction:.0f}%",
            va="center",
            ha="left",
            fontsize=9,
            color=PALETTE["dark"],
        )
    ax.legend(loc="lower right")
    ax.grid(axis="x", color=PALETTE["light_gray"], linewidth=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    return save_figure(fig, out_dir, "e2e_latency_baseline_vs_damer", formats)


def event_rows(report: Dict[str, object]) -> List[Dict[str, object]]:
    rows = []
    for workload in report["workloads"]:
        for case in workload["cases"]:
            latency = case["latency_model"]
            rows.append(
                {
                    "workload": workload["name"],
                    "switchlet": case["switchlet"],
                    "kind": case["kind"],
                    "speedup": float(latency["speedup"]),
                    "baseline_us": float(latency["baseline_us"]),
                    "damer_us": float(latency["damer_us"]),
                }
            )
    return rows


def plot_event_speedups(report: Dict[str, object], out_dir: str, formats: Sequence[str]) -> List[str]:
    rows = sorted(event_rows(report), key=lambda row: (str(row["workload"]), -float(row["speedup"])))
    labels = [
        textwrap.shorten(str(row["switchlet"]).replace("_", " "), width=38, placeholder="...")
        for row in rows
    ]
    speedups = [float(row["speedup"]) for row in rows]
    colors = [KIND_COLORS.get(str(row["kind"]), PALETTE["gray"]) for row in rows]
    y_positions = list(range(len(rows)))

    fig_height = max(7.0, 0.25 * len(rows) + 1.4)
    fig, ax = plt.subplots(figsize=(8.4, fig_height))
    ax.barh(y_positions, speedups, color=colors, height=0.72)
    ax.axvline(1.0, color="#8792A2", linewidth=1.0, linestyle="--")
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=7.5)
    ax.invert_yaxis()
    ax.set_xlabel("Modeled event speedup")
    ax.set_title("Event-Level Speedups by Movement Primitive")
    for y, value in zip(y_positions, speedups):
        ax.text(value + 0.05, y, f"{value:.1f}x", va="center", ha="left", fontsize=7.5)

    legend_items = [
        Patch(facecolor=color, label=kind.replace("move+", "+"))
        for kind, color in KIND_COLORS.items()
        if any(row["kind"] == kind for row in rows)
    ]
    ax.legend(handles=legend_items, loc="lower right", ncol=2, fontsize=8)
    ax.grid(axis="x", color=PALETTE["light_gray"], linewidth=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    return save_figure(fig, out_dir, "e2e_event_speedups", formats)


def plot_all(args: argparse.Namespace) -> Dict[str, object]:
    setup_style()
    report = load_report(os.path.abspath(args.report))
    out_dir = os.path.abspath(args.out_dir)
    ensure_dir(out_dir)
    formats = args.format or ["svg", "png"]

    outputs = []
    outputs.extend(plot_workload_speedup(report, out_dir, formats))
    outputs.extend(plot_latency_comparison(report, out_dir, formats))
    outputs.extend(plot_event_speedups(report, out_dir, formats))

    manifest = {
        "ok": True,
        "kind": "damer.e2e.speedup.figures",
        "source_report": os.path.abspath(args.report),
        "output_dir": out_dir,
        "figures": outputs,
    }
    manifest_path = os.path.join(out_dir, "figures_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    manifest["manifest"] = manifest_path
    return manifest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot Damer E2E speedup figures")
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--format", action="append", choices=["svg", "png", "pdf"])
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = plot_all(args)
    print(
        "E2E_FIGURES ok=true figures={count} out_dir={out_dir}".format(
            count=len(manifest["figures"]),
            out_dir=manifest["output_dir"],
        )
    )
    for path in manifest["figures"]:
        print(f"E2E_FIGURE path={path}")
    print(f"E2E_FIGURES_MANIFEST path={manifest['manifest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
