#!/usr/bin/env python3
"""Recompute sharing/lock/collaboration metrics and audit raw event traces."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

from store import dump


def percentile(xs, q):
    return sorted(xs)[max(0, math.ceil(len(xs)*q)-1)] if xs else None


def analyze_events(events):
    events = [e for e in events if e["kind"] in {"controlled", "opencode-live"}]
    if len({e["event_id"] for e in events}) != len(events):
        raise ValueError("duplicate event IDs")
    good = [e for e in events if e["status"] == "ok"]
    versions, readers, users, writers = {}, defaultdict(set), defaultdict(set), defaultdict(set)
    edges = Counter()
    for e in events:
        if e["acquired_ns"] is not None:
            assert e["request_ns"] <= e["acquired_ns"] <= e["released_ns"] <= e["end_ns"]
            # released_ns is recorded just AFTER unlock; use publication/access
            # metadata for version checks rather than claiming exact unlock time.
            assert e["wait_ns"] >= 0 and e["hold_ns"] >= 0
        if e["status"] != "ok":
            continue
        users[e["object"]].add(e["agent"])
        if "version_written" in e:
            k = (e["object"], e["version_written"])
            if k in versions:
                raise ValueError("duplicate committed version (lost update)")
            assert e["version_written"] == e["version_read"]+1
            versions[k] = e
            writers[e["object"]].add(e["agent"])
        if e["op"] == "read":
            readers[(e["object"],e["version_read"])].add(e["agent"])
            if e["producer"] not in {"harness", e["agent"]}:
                edges[(e["producer"], e["agent"], e["object"])] += 1
    for e in good:
        if e["producer"] != "harness":
            producer = versions.get((e["object"], e["version_read"]))
            if producer is None or producer["agent"] != e["producer"]:
                raise ValueError("read references missing/wrong producer")
            assert producer["acquired_ns"] <= e["acquired_ns"]
    reads = [e for e in good if e["op"] == "read"]
    shared_reads = [e for e in reads if len(readers[(e["object"],e["version_read"])]) >= 2]
    # Known nonnegative page counts are checked against config separately.
    waits = [e["wait_ns"] for e in events if e["acquired_ns"] is not None]
    holds = [e["hold_ns"] for e in events if e["acquired_ns"] is not None]
    contention = [e["contended"] for e in events if e["contended"] is not None]
    return dict(events=len(events), successful=len(good), errors=sum(e["status"]=="error" for e in events),
                conflicts=sum(e["status"]=="conflict" for e in events), objects=len(users),
                shared_objects=sum(len(v)>=2 for v in users.values()),
                multiwriter_objects=sum(len(v)>=2 for v in writers.values()),
                readonly_operations=len(reads), shared_version_read_operations=len(shared_reads),
                shared_read_ratio=len(shared_reads)/len(reads) if reads else None,
                bytes_scanned=sum(e["read_bytes"] for e in good),
                bytes_written=sum(e["write_bytes"] for e in good),
                bytes_copied=sum(e["copy_bytes"] for e in good),
                response_bytes=sum(e["response_bytes"] for e in events),
                lock_contended=sum(contention), lock_attempts=len(contention),
                contention_ratio=sum(contention)/len(contention) if contention else None,
                wait_p50_ns=percentile(waits,.5), wait_p95_ns=percentile(waits,.95),
                wait_total_ns=sum(waits), hold_total_ns=sum(holds),
                wait_share_of_lock_time=sum(waits)/(sum(waits)+sum(holds)) if waits else None,
                cross_agent_read_operations=sum(edges.values()),
                collaboration_edges=[dict(producer=a, consumer=b, object=o, reads=n)
                                     for (a,b,o),n in sorted(edges.items())])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("output", type=Path)
    args = p.parse_args()
    root = args.output
    manifest = json.loads((root/"manifest.json").read_text())
    for name,digest in manifest["source_sha256"].items():
        saved=root/"source"/name
        if not saved.exists():saved=Path(__file__).parent/name
        assert hashlib.sha256(saved.read_bytes()).hexdigest()==digest, f"source provenance mismatch: {name}"
    runs = [json.loads(line) for line in (root/"runs.jsonl").read_text().splitlines()]
    summaries, groups = [], defaultdict(list)
    for r in runs:
        if r["status"] != "ok":
            continue
        config = json.loads((root/r["run_id"]/"store.json").read_text())
        events = [json.loads(line) for p in sorted((root/r["run_id"]/"events").glob("*.jsonl"))
                  for line in p.read_text().splitlines()]
        for e in events:
            if e["status"] == "ok":
                assert e["nodes"] == {str(config["node"]):config["capacity"]//4096}
                if "lock_node" in config:
                    assert e["lock_nodes"] == {str(config["lock_node"]):1}
                if e["copy_bytes"]:
                    assert e["private_nodes"] == {str(r["copy_node"]):config["capacity"]//4096}
        metrics = analyze_events(events)
        # Independently check every object's committed versions, including
        # independent writers, without trusting the runner's success label.
        for key in ["ledger", *[f"note-{i}" for i in range(r["workers"])]]:
            versions=sorted(e["version_written"] for e in events if e["kind"]=="controlled" and
                            e["object"]==key and e["status"]=="ok" and "version_written" in e)
            expected=0
            if key.startswith("note-") and r["workload"] in {"independent","collaboration"}:expected=r["steps"]
            if key=="ledger" and r["workload"]=="collaboration":expected=r["steps"]*r["workers"]
            assert versions==list(range(1,expected+1)), f"missing version in {r['run_id']}/{key}"
        ops_per_step = 4 if r["workload"]=="collaboration" else 1
        assert metrics["events"] == r["workers"]*r["steps"]*ops_per_step
        assert metrics["errors"] == metrics["conflicts"] == 0
        if r["workload"] == "collaboration" and r["workers"] > 1:
            assert metrics["cross_agent_read_operations"] == r["workers"]*r["steps"]
        row = dict(**r, **metrics)
        summaries.append(row)
        groups[(r["node"],r["workers"],r["granularity"],r["workload"],r["copy_node"])].append(row)
    aggregate = []
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        node, n, g, workload, copy = key
        aggregate.append(dict(node=node, workers=n, granularity=g, workload=workload, copy_node=copy,
                         runs=len(rows), median_ms=statistics.median(r["duration_ns"]/1e6 for r in rows),
                         min_ms=min(r["duration_ns"]/1e6 for r in rows),
                         max_ms=max(r["duration_ns"]/1e6 for r in rows),
                         median_contention_ratio=statistics.median(r["contention_ratio"] for r in rows),
                         median_wait_p95_us=statistics.median(r["wait_p95_ns"]/1000 for r in rows),
                         median_shared_pss_kib=statistics.median(r["shared_mapping_pss_kib"] for r in rows),
                         cross_agent_reads=sum(r["cross_agent_read_operations"] for r in rows),
                         errors=sum(r["errors"] for r in rows)))
    dump(root/"summary.json", dict(run_status=dict(Counter(r["status"] for r in runs)),
                                  aggregate=aggregate, runs=summaries))
    if aggregate:
        with (root/"summary.csv").open("w") as f:
            writer=csv.DictWriter(f, fieldnames=list(aggregate[0]))
            writer.writeheader()
            writer.writerows(aggregate)
    assert len(runs) == manifest["planned_runs"]
    validation = dict(assessment="Share with caveats" if len(summaries)==len(runs) else "Needs revision",
                      planned_runs=len(runs), successful_runs=len(summaries),
                      measured_events=sum(r["events"] for r in summaries),
                      checks=["source snapshot hashes", "event uniqueness", "complete operation counts", "NUMA residency",
                              "source-version producer linkage", "contiguous committed versions", "checksums", "ledger contents"],
                      limitations=["Controlled processes, not observed OpenCode agents or DSec",
                                   "Warm-cache tool path timings; not raw CXL bandwidth or LLM end-to-end speedup",
                                   "Single host; Python/metadata/placement instrumentation included",
                                   "Sharing and cooperation are prescribed by workload; their rates are not discovery",
                                   "PSS covers live shared mappings only; excludes retained unmapped backing and transient copy buffers",
                                   "Repeated runs are descriptive; operation p95 is not an independent-trial confidence interval"])
    dump(root/"validation.json", validation)
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
