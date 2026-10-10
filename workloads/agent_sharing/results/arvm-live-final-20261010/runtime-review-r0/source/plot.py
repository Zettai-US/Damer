#!/usr/bin/env python3
"""Standalone research figure from checked observations, including every repeat.

Chart contract: compare lock granularity / copying in three prescribed workloads;
grouped bars show five-run median, whiskers full observed range, dots all runs.
3 worker counts are discrete experimental categories, not a fitted trend.
One blue root plus open fills/hatches; zero baseline; PNG/PDF for paper use.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path)
    p.add_argument("--node",type=int,default=1)
    a=p.parse_args()
    d=json.loads((a.output/"summary.json").read_text())
    rows=d["runs"]
    manifest=json.loads((a.output/"manifest.json").read_text())
    ns=sorted({r["workers"] for r in rows})
    copy_nodes={r["copy_node"] for r in rows if r["node"]==a.node and r["copy_node"] is not None}
    if len(copy_nodes)!=1:raise ValueError("figure requires exactly one private-copy comparison node")
    copy_node=next(iter(copy_nodes))
    fig,axes=plt.subplots(1,3,figsize=(11.5,3.9))
    blue,ink="#4477AA","#222222"
    for ax,workload,title in zip(axes,["independent","collaboration","fanout"],
                                ["Independent updates","Producer / consumer workflow","Shared reads vs fresh copies"]):
        variants=[("global",None,"Global RW lock"),("object",None,"Object RW lock")]
        if workload=="fanout":variants=[("object",None,"Shared CXL"),("object",copy_node,"Fresh DRAM copy")]
        for j,(g,copy,label) in enumerate(variants):
            values=[[r["duration_ns"]/1e6 for r in rows if r["node"]==a.node and r["workers"]==n
                     and r["workload"]==workload and r["granularity"]==g and r["copy_node"]==copy] for n in ns]
            med=[statistics.median(v) for v in values]
            x=[i+(j-.5)*.34 for i in range(len(ns))]
            ax.bar(x,med,width=.32,label=label,color=blue if j==0 else "white",edgecolor=ink,
                   hatch=None if j==0 else "///",linewidth=.8)
            ax.errorbar(x,med,yerr=[[m-min(v) for m,v in zip(med,values)],[max(v)-m for m,v in zip(med,values)]],
                        fmt="none",color=ink,capsize=3,linewidth=.8)
            for xx,v in zip(x,values):
                ax.scatter([xx+(k-(len(v)-1)/2)*.024 for k in range(len(v))],v,
                           s=9,c="white" if j==0 else ink,edgecolors=ink,linewidths=.4,zorder=4)
        ax.set_title(title,fontsize=11)
        ax.set_xticks(range(len(ns)),ns)
        ax.set_xlabel("Worker processes")
        ax.set_ylabel("Episode wall time (ms)")
        ax.set_ylim(bottom=0)
        ax.spines[["top","right"]].set_visible(False)
        ax.grid(axis="y",alpha=.18)
        ax.set_axisbelow(True)
        ax.legend(frameon=False,fontsize=8,loc="lower left",bbox_to_anchor=(0,1.01))
    capacity=sorted({r["capacity_bytes"]//1024 for r in rows})
    steps=sorted({r["steps"] for r in rows})
    repeats=sorted({r["runs"] for r in d["aggregate"]})
    backends=sorted({r["backend"] for r in rows})
    fig.suptitle("CXL sharing and lock controls | "+manifest["started_utc"][:10],fontsize=13,y=.99)
    fig.text(.5,.01,f"Controlled processes, {', '.join(backends)}; {capacity} KiB/object, {steps} steps, {repeats} repeats. Median bars; range whiskers; all-run dots.\nWarm caches; Python + metadata + trace overhead included. Not live OpenCode or raw CXL latency.",
             ha="center",fontsize=8)
    for ax in axes:ax.set_title(ax.get_title(),fontsize=11,pad=38)
    fig.tight_layout(rect=(0,.11,1,.90))
    for ext in ["png","pdf"]:fig.savefig(a.output/f"motivation-controls.{ext}",dpi=180)
    (a.output/"figure-provenance.json").write_text(json.dumps({
        "plot_source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "summary_sha256":hashlib.sha256((a.output/"summary.json").read_bytes()).hexdigest(),
        "node":a.node,"statistic":"median; full min/max range; individual-run dots",
        "source":"summary.json runs; controlled process experiments",
    },indent=2)+"\n")


if __name__=="__main__":main()
