#!/usr/bin/env python3
"""Real OpenCode review/peer-review/synthesis with CXL sharing and reclamation."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from store import Store,dump
from dsec import DSec
from dataflow import compile_memory_contract
from run_live import invoke

SRC=Path(__file__).resolve().parent
MODEL="local/damer-qwen35"


def plan(workers,keys):
    tasks={}
    for i in range(workers):
        tasks[f"draft-{i}"]=dict(actor=f"reviewer-{i}",phase="draft",depends_on=[],
            grants=dict(read=keys,write=[f"draft-{i}"],append=[f"scratch-{i}","ledger"]),
            effects=dict(read=["corpus",f"draft-{i}"],write=[f"draft-{i}"],append=[f"scratch-{i}","ledger"]))
    for i in range(workers):
        tasks[f"review-{i}"]=dict(actor=f"reviewer-{i}",phase="peer-review",depends_on=[f"draft-{j}" for j in range(workers)],
            grants=dict(read=keys,write=[f"review-{i}"],append=["ledger"]),
            effects=dict(read=["corpus",f"draft-{i}",f"draft-{(i+1)%workers}",f"review-{i}"],
                         write=[f"review-{i}"],append=["ledger"]))
    tasks["synthesis"]=dict(actor="synthesizer",phase="synthesis",depends_on=[f"review-{i}" for i in range(workers)],
        grants=dict(read=keys,write=["answer"]),
        effects=dict(read=["corpus","ledger","answer",*[f"review-{i}" for i in range(workers)]],write=["answer"]))
    return tasks


def prompts(workers):
    topics=[("admission and scope","def admit"),("retirement and pin lifetime","def retire"),
            ("version validation and publication","expected_version"),("dependencies and reclamation","retention_until_tasks_complete")]
    result={}
    for i in range(workers):
        topic,query=topics[i%len(topics)]
        result[f"draft-{i}"]=(f"You are reviewer {i}. Review {topic} in the real source snapshot. Execute ALL steps: "
            f"1. sharing_search corpus for '{query}'. 2. sharing_append a short source-grounded working note to scratch-{i}. "
            f"3. sharing_read draft-{i} and sharing_write your finding there using expected_version. "
            f"4. sharing_append one line 'draft-{i}: ...' to ledger. "
            "Give a concrete supported observation and a limitation; do not invent bugs. Each note under 700 characters. Use tools now.")
        result[f"review-{i}"]=(f"You are reviewer {i} performing peer review. Execute ALL steps: "
            f"1. sharing_read draft-{i} and draft-{(i+1)%workers}. "
            f"2. sharing_search corpus for '{query}' to check a claim. "
            f"3. sharing_read review-{i} and sharing_write a revised conclusion there with expected_version, "
            f"explicitly mention what you retained or corrected from your peer. 4. sharing_append 'review-{i}: ...' to ledger. "
            "Each note under 700 characters. Do not merely describe the tools.")
    result["synthesis"]=("Use sharing_read to read each of "+", ".join(f"review-{i}" for i in range(workers))+
        ". Also read ledger and use sharing_search on corpus for 'def retire' to verify the source. "
        "Distinguish implemented guarantees from limitations and propose one test. Read answer, then publish a concise synthesis "
        "under 1500 characters using sharing_write with expected_version. Execute every listed read, not only the first finding.")
    return result


def configure(workspace,store,role):
    workspace.mkdir()
    config=dict(model=MODEL,snapshots=False,
        providers={"local":dict(name="local",env=["DAMER_LOCAL_API_KEY"],package="@opencode/ai/providers/openai-compatible",
            settings={"baseURL":"http://127.0.0.1:18081/v1"},
            models={"damer-qwen35":dict(name="damer-qwen35",capabilities={"tools":True,"input":["text"],"output":["text"]},
                                      limit={"context":16384,"output":2048})})},
        permissions=[dict(action="*",resource="*",effect="deny"),dict(action="sharing_*",resource="*",effect="allow")],
        mcp={"servers":{"sharing":dict(type="local",codemode=False,
            command=[sys.executable,str(SRC/"mcp_server.py"),"--store",str(store.root),"--agent",role])}})
    dump(workspace/"opencode.json",config)
    (workspace/"AGENTS.md").write_text("Only use sharing MCP tools. Follow every requested read and publication. Keep notes concise. Do not invoke shell, files, network, or other agents.\n")


def all_events(store):
    return [json.loads(line) for f in (store.root/"events").glob("*.jsonl") for line in f.read_text().splitlines()]


def check_task(store,role,spec):
    events=[e for e in all_events(store) if e["agent"]==role and e["kind"]=="opencode-live" and e["status"]=="ok"]
    observed={(e["op"],e["object"]) for e in events}
    required={(op,key) for op,keys in spec["effects"].items() for key in keys}
    return dict(complete=required<=observed,missing=sorted(required-observed),events=len(events))


def checkpoint(store,dsec,out,label):
    with dsec.registry() as state:
        snapshot=dict(ns=time.monotonic_ns(),label=label,retention_policy=state["retention_policy"],
            tasks={name:t["state"] for name,t in state["sandboxes"].items()},retired=state["retired"],
            active_pins=len(state["pins"]),session_roots=state["session_roots"])
    root=Path(store.config["payload_root"])
    objects={p.name:dict(capacity=p.stat().st_size,allocated_bytes=p.stat().st_blocks*512,
                        inode=p.stat().st_ino,device=p.stat().st_dev) for p in root.iterdir() if p.is_file()}
    snapshot.update(objects=objects,payload_bytes=sum(v["allocated_bytes"] for v in objects.values()),
        control_bytes=sum(p.stat().st_blocks*512 for dirname in [".records",".locks"] for p in (root/dirname).iterdir()))
    with (out/"memory.jsonl").open("a") as f:f.write(json.dumps(snapshot)+"\n")


def collect_dead(store,dsec,out):
    for key in store.list_objects():
        if not (Path(store.config["payload_root"])/key).exists():continue
        try:dsec.reclaim(key)
        except RuntimeError as exc:
            if not any(word in str(exc) for word in ["retained","pinned"]):raise
        else:checkpoint(store,dsec,out,"reclaimed:"+key)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--workers",type=int,choices=[2,4],default=2)
    p.add_argument("--retention",choices=["scope","dataflow"],default="dataflow")
    p.add_argument("--capacity-kib",type=int,default=64)
    p.add_argument("--opencode",default="/home/jason/.opencode/bin/opencode")
    p.add_argument("--timeout",type=int,default=240)
    a=p.parse_args()
    if not 64<=a.capacity_kib<=1024 or a.capacity_kib%4:p.error("capacity: 64..1024 KiB page aligned")
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ.setdefault("DAMER_LOCAL_API_KEY","local-experiment")
    binary=SRC/"build/opencode"
    if not binary.exists():shutil.copy2(shutil.which(a.opencode),binary)
    payload=Path("/dev/shm")/("arvm-profile-"+uuid.uuid4().hex)
    corpus_paths=[SRC/"fixtures/arvm_registry.py",SRC/"store.py",SRC/"dsec.py",SRC/"dataflow.py"]
    corpus="Actual arVM prototype source snapshot. Inspect line context; these files are distinct modules.\n"+"\n".join(
        f"\n===== {f.name} =====\n"+f.read_text() for f in corpus_paths)
    keys=["corpus","ledger","answer",*[f"{prefix}-{i}" for prefix in ["scratch","draft","review"] for i in range(a.workers)]]
    tasks=plan(a.workers,keys);instructions=prompts(a.workers)
    begin=time.monotonic_ns();store=None
    manifest=dict(schema="arvm-sharing-profile-v1",kind="opencode-live-multiphase",workers=a.workers,
        retention_policy=a.retention,model=MODEL,status="running",task_sessions=2*a.workers+1,
        started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        opencode_version=__import__('subprocess').check_output([str(binary),'--version'],text=True).strip(),
        task_results=[],capacity_kib=a.capacity_kib,
        source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in SRC.glob("*.py")},
        corpus_sources={str(f.relative_to(SRC)):hashlib.sha256(f.read_bytes()).hexdigest() for f in corpus_paths},
        scope="prescribed accepted effects with broad project read scope; tool-process shared memory, copied model responses")
    (out/"source").mkdir()
    for name in manifest["source_sha256"]:shutil.copy2(SRC/name,out/"source"/name)
    (out/"corpus.txt").write_text(corpus)
    try:
        store=Store.create(out/"store",payload,node=1,capacity=a.capacity_kib*1024)
        for key in keys:store.seed(key,corpus if key=="corpus" else "")
        started_compile=time.monotonic_ns()
        contract=compile_memory_contract(tasks,{key:0 for key in keys})
        manifest["compile_ns"]=time.monotonic_ns()-started_compile
        dump(out/"memory-contract.json",contract)
        dsec=DSec.initialize(store,tasks,retention_policy=a.retention,session_roots=["answer"])
        for role in tasks:configure(out/role,store,role)
        checkpoint(store,dsec,out,"allocated")
        phases=[[f"draft-{i}" for i in range(a.workers)],[f"review-{i}" for i in range(a.workers)],["synthesis"]]
        for phase in phases:
            for role in phase:dsec.admit(role)
            checkpoint(store,dsec,out,"phase-start:"+phase[0])
            with ThreadPoolExecutor(max_workers=a.workers) as executor:
                pending={executor.submit(invoke,str(binary),out/role,MODEL,instructions[role],a.timeout,store.root):role for role in phase}
                for future in as_completed(pending):
                    role=pending[future]
                    result=future.result();result["protocol"]=check_task(store,role,tasks[role])
                    # Admission safety depends on publication, while complete
                    # prompt compliance is separately audited and retained.
                    outputs=tasks[role]["effects"].get("write",[])
                    published=all(json.loads((store.root/"meta"/(k+".json")).read_text())["version"]>0 for k in outputs)
                    result["published"]=published
                    success=result["process_ok"] and published
                    dsec.finish(role,success)
                    manifest["task_results"].append(result)
                    checkpoint(store,dsec,out,"task-finish:"+role)
                    collect_dead(store,dsec,out)
                    dump(out/"manifest.json",manifest)
                    print(json.dumps(dict(task=role,policy=a.retention,success=success,protocol=result["protocol"])),flush=True)
            if not all(r["process_ok"] and r["published"] for r in manifest["task_results"]):
                manifest["status"]="failed-task";break
        else:
            store.trace_kind="verification"
            dump(out/"answer.json",store.access("answer"))
            dsec.release_root("answer");collect_dead(store,dsec,out)
            checkpoint(store,dsec,out,"complete")
            manifest["status"]="complete"
        dsec.snapshot()
        manifest["protocol_complete"]=all(r["protocol"]["complete"] for r in manifest["task_results"]) and manifest["status"]=="complete"
    except BaseException as exc:
        manifest.update(status="failed",error=repr(exc));raise
    finally:
        manifest["duration_ns"]=time.monotonic_ns()-begin
        manifest["finished_utc"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        dump(out/"manifest.json",manifest)
        if store:store.close()
        if payload.exists():shutil.rmtree(payload)
    return 0 if manifest["status"]=="complete" else 2


if __name__=="__main__":raise SystemExit(main())
