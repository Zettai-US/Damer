#!/usr/bin/env python3
"""Bounded randomized CXL/DRAM control experiments; these are NOT LLM agents."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import shutil
import sys
import time
import uuid

from store import Store, dump, hardware


def worker(root, agent, count, steps, workload, barrier, conn, cpu, copy_node):
    store = None
    try:
        os.sched_setaffinity(0, {cpu})
        store = Store(root, f"worker-{agent}", "warmup")
        store.access("corpus", return_limit=0)
        store.trace_kind = "controlled"
        conn.send({"ready": True})
        barrier.wait(timeout=30)
        start = time.monotonic_ns()
        for step in range(steps):
            if workload == "fanout":
                store.access("corpus", return_limit=0, copy_node=copy_node)
            elif workload == "independent":
                store.access(f"note-{agent}", "append", text=f"{agent}:{step}\n", return_limit=0)
            else:
                store.access("corpus", return_limit=0)
                store.access(f"note-{agent}", "append", text=f"{agent}:{step}\n", return_limit=0)
                store.access("ledger", "append", text=f"{agent}:{step}\n", return_limit=0)
                # A controlled synchronization point, not inferred LLM behavior.
                barrier.wait(timeout=30)
                store.access(f"note-{(agent+1)%count}", return_limit=0)
                barrier.wait(timeout=30)
        end = time.monotonic_ns()
        conn.send(dict(ok=True, start_ns=start, end_ns=end, pid=os.getpid(), cpu=cpu))
        if not conn.poll(30):
            raise TimeoutError("parent failed to release worker")
        conn.recv()
    except BaseException as exc:
        conn.send(dict(ok=False, error=repr(exc)))
        barrier.abort()
    finally:
        if store:
            store.close()
        conn.close()


def payload_pss(pid, payload_root):
    total, active = 0, False
    for line in Path(f"/proc/{pid}/smaps").read_text().splitlines():
        if line and line[0] in "0123456789abcdef" and "-" in line.split()[0]:
            active = str(payload_root) + "/" in line
        elif active and line.startswith("Pss:"):
            total += int(line.split()[1])
    return total


def free_bytes(node):
    for line in Path(f"/sys/devices/system/node/node{node}/meminfo").read_text().splitlines():
        if "MemFree:" in line:
            return int(line.split()[-2]) * 1024
    raise RuntimeError("missing NUMA memory inventory")


def run_one(out, config, args):
    root = out / config["run_id"]
    payload = Path("/dev/shm") / ("damer-sharing-" + uuid.uuid4().hex)
    n = config["workers"]
    store = None
    children, pipes = [], []
    try:
        store = Store.create(root, payload, node=config["node"], capacity=args.capacity_kib*1024,
                             granularity=config["granularity"], lock_node=args.lock_node,
                             require_cxl=config["node"] == args.cxl_node)
        evidence = "Evidence: worker results must preserve every contributor.\n"
        store.seed("corpus", (evidence * (args.capacity_kib*1024//len(evidence))).strip())
        for key in ["ledger", *[f"note-{i}" for i in range(n)]]:
            store.seed(key, "")
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(n + 1)
        cpus = sorted(os.sched_getaffinity(0))[:n]
        for agent in range(n):
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=worker, args=(root, agent, n, args.steps, config["workload"],
                               barrier, child, cpus[agent], config["copy_node"]))
            proc.start()
            child.close()
            children.append(proc)
            pipes.append(parent)
        for conn in pipes:
            if not conn.poll(30) or not conn.recv().get("ready"):
                raise RuntimeError("worker did not finish warmup")
        begin = time.monotonic_ns()
        barrier.wait(timeout=30)
        # The parent participates only at the initial dispatch barrier and at
        # deterministic producer/consumer steps in the collaboration fixture.
        if config["workload"] == "collaboration":
            for _ in range(args.steps):
                barrier.wait(timeout=30)
                barrier.wait(timeout=30)
        workers = []
        for conn in pipes:
            if not conn.poll(30):
                raise TimeoutError("worker timed out")
            result = conn.recv()
            if not result["ok"]:
                raise RuntimeError(result)
            workers.append(result)
        end = max(w["end_ns"] for w in workers)
        pss = sum(payload_pss(w["pid"], payload) for w in workers)
        for conn in pipes:
            conn.send("release")
        for proc in children:
            proc.join(10)
            if proc.exitcode != 0:
                raise RuntimeError(f"worker exit {proc.exitcode}")
        store.trace_kind = "verification"
        if config["workload"] in {"independent", "collaboration"}:
            for i in range(n):
                note=store.access(f"note-{i}", return_limit=65536)
                if note["version"] != args.steps or note["text"].splitlines() != [f"{i}:{s}" for s in range(args.steps)]:
                    raise AssertionError("lost or duplicated note updates")
        if config["workload"] == "collaboration":
            actual = store.access("ledger", return_limit=65536)["text"].splitlines()
            expected = [f"{i}:{step}" for i in range(n) for step in range(args.steps)]
            if Counter(actual) != Counter(expected):
                raise AssertionError("lost or duplicated ledger updates")
        result = dict(**config, status="ok", kind="controlled", steps=args.steps,
                      capacity_bytes=args.capacity_kib*1024, duration_ns=end-begin,
                      worker_times=workers, shared_mapping_pss_kib=pss,
                      allocated_payload_bytes=(n+2)*args.capacity_kib*1024,
                      backend=store.backend.name, checksum_ok=True, lost_updates=0)
        dump(root / "result.json", result)
        return result
    finally:
        for proc in children:
            if proc.is_alive():
                proc.terminate()
            proc.join(5)
        for conn in pipes:
            conn.close()
        if store:store.close()
        if payload.exists():shutil.rmtree(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cxl-node", type=int, default=1)
    p.add_argument("--dram-node", type=int, default=0)
    p.add_argument("--workers", nargs="+", type=int, default=[1,2,4])
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--capacity-kib", type=int, default=256)
    p.add_argument("--seed", type=int, default=20261010)
    p.add_argument("--lock-node", type=int, default=1, help="Verified CXL node for every lock")
    args = p.parse_args()
    if not args.workers or min(args.workers)<1 or max(args.workers)>8 or args.repeats<1 or not 1<=args.steps<=100:
        p.error("workers 1..8, repeats >=1, steps 1..100 required")
    if not 4<=args.capacity_kib<=4096 or args.capacity_kib%4:
        p.error("capacity must be a 4 KiB multiple, 4..4096 KiB")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(args.seed)
    configs = [dict(node=node, workers=n, granularity=g, workload=w, copy_node=None)
               for node,n,g,w in itertools.product([args.dram_node,args.cxl_node], args.workers,
                         ["global","object"], ["fanout","independent","collaboration"])]
    configs += [dict(node=args.cxl_node, workers=n, granularity="object", workload="fanout",
                     copy_node=args.dram_node) for n in args.workers]
    source_dir = Path(__file__).parent
    manifest = dict(schema=1, kind="controlled-not-llm", started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    argv=sys.argv, seed=args.seed, hardware=[hardware(i) for i in [args.dram_node,args.cxl_node]],
                    source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in source_dir.glob("*.py")},
                    planned_runs=len(configs)*args.repeats, python=sys.version,
                    notes=["warm caches; one untimed corpus warmup per worker", "not a dedicated host",
                           "fresh private-copy buffer per read; no retained-copy baseline",
                           "Python checksums, metadata I/O and placement checks included in lock hold time"])
    (out/"source").mkdir()
    for name in manifest["source_sha256"]:
        shutil.copyfile(source_dir/name, out/"source"/name)
    dump(out/"manifest.json", manifest)
    statuses=Counter()
    with (out/"runs.jsonl").open("w") as f:
        for block in range(args.repeats):
            rng.shuffle(configs)
            for index, config in enumerate(configs):
                config = dict(config, block=block, run_id=f"block-{block:02d}-case-{index:02d}")
                needed = (config["workers"]+2)*args.capacity_kib*1024 + 48*1024*1024
                if free_bytes(args.dram_node) < needed:
                    result = dict(**config, status="skipped", reason="DRAM free-memory margin")
                else:
                    try:
                        result = run_one(out, config, args)
                    except Exception as exc:
                        result = dict(**config, status="failed", error=repr(exc))
                f.write(json.dumps(result)+"\n")
                statuses[result["status"]]+=1
                f.flush()
                if result["status"] != "ok":
                    print(json.dumps(result), flush=True)
            print(f"block {block+1}/{args.repeats} complete", flush=True)
    manifest["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    manifest["run_status"]=dict(statuses)
    dump(out/"manifest.json",manifest)
    return 0 if statuses["ok"]==manifest["planned_runs"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
