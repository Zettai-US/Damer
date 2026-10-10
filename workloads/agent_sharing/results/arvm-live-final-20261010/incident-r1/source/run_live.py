#!/usr/bin/env python3
"""Run two OpenCode analysts plus a synthesizer against a shared CXL MCP store.

Requires OpenCode v2 and working provider credentials in the caller's environment
or OpenCode account. Emits failures instead of substituting simulated agents.
"""
import argparse
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import selectors
import signal
import subprocess
import sys
import time
import uuid
import urllib.parse
import urllib.request

from store import Store, dump
from analyze import analyze_events
from dsec import DSec, sandbox_command
from dataflow import compile_memory_contract

EVIDENCE = """Synthetic incident evidence for a controlled collaboration task:
10:00 release r42 enabled a new retry policy and a smaller cache.
10:01 requests=10000 errors=120 p95_ms=180 cache_hit_pct=92 retries=110.
10:03 requests=10000 errors=810 p95_ms=940 cache_hit_pct=51 retries=2200.
10:04 dependency CPU reached 96%; no dependency release was recorded.
10:06 rollback r42 began.
10:08 requests=10000 errors=140 p95_ms=210 cache_hit_pct=90 retries=170.
These are supplied fictional task inputs, NOT measured system performance.
Goal: distinguish observations from causal hypotheses and propose one check.
"""


@contextmanager
def private_server(binary, workspace, store_root=None):
    """Own server and descendants; keep its temporary password out of artifacts."""
    proc=subprocess.Popen(sandbox_command([binary,"serve","--hostname","127.0.0.1","--port","0"],workspace,store_root),cwd=workspace,
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
    selector=selectors.DefaultSelector()
    selector.register(proc.stdout,selectors.EVENT_READ)
    buffer=b""
    url=password=None
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and (url is None or password is None):
            if proc.poll() is not None:raise RuntimeError("private OpenCode server exited")
            for key,_ in selector.select(.2):
                buffer+=os.read(key.fd,4096)
                while b"\n" in buffer:
                    line,buffer=buffer.split(b"\n",1)
                    line=line.decode(errors="replace")
                    if line.startswith("server listening on "):url=line.split(" on ",1)[1].strip()
                    if line.startswith("server password "):password=line.split("password ",1)[1].strip()
        if url is None:raise TimeoutError("private server did not report a URL")
        query=urllib.parse.urlencode({"location[directory]":str(workspace)})
        def request(path, method="GET", body=None):
            headers={"Content-Type":"application/json"}
            if password:
                headers["Authorization"]="Basic "+base64.b64encode(("opencode:"+password).encode()).decode()
            req=urllib.request.Request(url+path+"?"+query,
                data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
            with urllib.request.urlopen(req,timeout=10) as response:
                raw=response.read()
                return json.loads(raw) if raw else None
        yield request,url,password
    finally:
        selector.close()
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait()
        proc.stdout.close()


def check_mcp(binary, workspace, config, store_root):
    """Give asynchronous MCP initialization time in an owned, temporary server."""
    with private_server(binary, workspace, store_root) as (request,_,_):
        request("/api/experimental/mcp/sharing","PUT",{"config":config})
        last=None
        for _ in range(100):
            last=request("/api/mcp")
            if "connected" in json.dumps(last).lower():break
            time.sleep(.1)
        return last


def invoke(binary, workspace, model, prompt, timeout, store_root=None):
    events_path, error_path = workspace/"opencode.jsonl", workspace/"stderr.log"
    (workspace/"prompt.txt").write_text(prompt+"\n")
    begin=time.monotonic_ns()
    with events_path.open("w") as out, error_path.open("w") as err, private_server(binary,workspace,store_root) as (request,url,password):
        config=json.loads((workspace/"opencode.json").read_text())
        for name,server in config.get("mcp",{}).get("servers",{}).items():
            request("/api/experimental/mcp/"+name,"PUT",{"config":server})
        if config.get("mcp"):
            for _ in range(100):
                if "connected" in json.dumps(request("/api/mcp")).lower():break
                time.sleep(.1)
            else:raise RuntimeError("MCP did not connect")
        # Avoid a private CLI startup racing lazy registry initialization.
        for _ in range(30):
            models=request("/api/model")
            if model.split("/",1)[-1] in json.dumps(models):break
            time.sleep(.1)
        env=os.environ.copy()
        if password:
            env["OPENCODE_SERVER_PASSWORD"]=password
            env["OPENCODE_PASSWORD"]=password
        argv=[binary,"run","--server",url,"--format","json","--model",model,prompt]
        proc=subprocess.Popen(sandbox_command(argv,workspace),cwd=workspace,stdout=out,stderr=err,env=env,start_new_session=True)
        timed_out=False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait()
        finally:
            # Private server and MCP descendants must not outlive this invocation.
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    errors=[]
    session_ids=set()
    for line in events_path.read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get("sessionID"):session_ids.add(event["sessionID"])
        if event.get("type")=="error":errors.append(event.get("error"))
    return dict(agent=workspace.name,returncode=proc.returncode,timeout=timed_out,
                duration_ns=time.monotonic_ns()-begin, errors=errors, session_ids=sorted(session_ids),
                process_ok=proc.returncode==0 and not timed_out and not errors)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--opencode",default="opencode")
    p.add_argument("--model",default="local/damer-qwen35")
    p.add_argument("--base-url",default="http://127.0.0.1:18081/v1")
    p.add_argument("--api-key-env",default="DAMER_LOCAL_API_KEY")
    p.add_argument("--cxl-node",type=int,default=1)
    p.add_argument("--capacity-kib",type=int,default=256)
    p.add_argument("--granularity",choices=["object","global"],default="object")
    p.add_argument("--lock-node",type=int,default=1)
    p.add_argument("--timeout",type=int,default=180)
    p.add_argument("--workload",choices=["runtime-review","incident"],default="runtime-review")
    p.add_argument("--check-mcp",action="store_true",help="Test OpenCode MCP connection without model requests")
    a=p.parse_args()
    binary=shutil.which(a.opencode)
    if not binary:p.error("OpenCode executable not found")
    version=subprocess.check_output([binary,"--version"],text=True).strip()
    if "v2." not in version:p.error("This runner requires OpenCode v2")
    if not 4<=a.capacity_kib<=4096 or a.capacity_kib%4:p.error("capacity: 4..4096 KiB, page aligned")
    if not 1<=a.timeout<=600:p.error("timeout: 1..600 seconds")
    out=a.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    payload=Path("/dev/shm")/("damer-live-"+uuid.uuid4().hex)
    src=Path(__file__).resolve().parent
    # The source executable can be in a private home directory. Copy the public
    # program into our runtime before dropping capabilities in user namespaces.
    local_binary=src/"build/opencode"
    if Path(binary).resolve()!=local_binary:
        if not local_binary.exists() or hashlib.sha256(local_binary.read_bytes()).digest()!=hashlib.sha256(Path(binary).read_bytes()).digest():
            shutil.copy2(binary,local_binary)
    binary=str(local_binary)
    os.environ.setdefault(a.api_key_env,"local-experiment") if a.base_url.startswith("http://127.0.0.1:") else None
    store=None
    manifest=dict(kind="opencode-live",opencode_version=version,model=a.model,
                  started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                  source_sha256={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in src.glob("*.py")},
                  status="running",roles=["analyst-0","analyst-1","synthesizer"],
                  workload=a.workload,behavior_scope="prescribed task graph; model selects and executes real tool calls")
    (out/"source").mkdir()
    for name in manifest["source_sha256"]:shutil.copyfile(src/name,out/"source"/name)
    dump(out/"manifest.json",manifest)
    try:
        store=Store.create(out/"store",payload,node=a.cxl_node,capacity=a.capacity_kib*1024,
                           granularity=a.granularity,lock_node=a.lock_node)
        evidence=EVIDENCE
        if a.workload=="runtime-review":
            code_path=src.parents[1]/"6ac4087cf755acfa13cf5a59/experiments/runtime.py"
            # Real, bounded source excerpt including the full Registry class.
            code=code_path.read_text().split("\nclass Compiler:",1)[0]
            evidence="Review this actual arVM registry prototype. Find a concrete correctness or scope issue; distinguish implemented guarantees from limitations.\n"+code
            if len(evidence.encode())>4096:raise ValueError("review fixture exceeds tool read limit")
            manifest["evidence_source"]=dict(path=str(code_path),sha256=hashlib.sha256(code_path.read_bytes()).hexdigest(),
                                            excerpt="module prefix through Registry, before Compiler")
        (out/"evidence.txt").write_text(evidence)
        store.seed("evidence",evidence)
        for key in ["ledger","finding-0","finding-1","synthesis"]:store.seed(key,"")
        specs={f"analyst-{i}":dict(depends_on=[],grants=dict(read=["evidence","ledger",f"finding-{i}"],
                       write=[f"finding-{i}"],append=["ledger"])) for i in range(2)}
        specs["synthesizer"]=dict(depends_on=["analyst-0","analyst-1"],
            grants=dict(read=["evidence","finding-0","finding-1","ledger","synthesis"],write=["synthesis"]))
        contract=compile_memory_contract(specs,{key:0 for key in store.list_objects()})
        dump(out/"arvm-memory-contract.json",contract)
        dsec=DSec.initialize(store,contract["tasks"])
        dump(out/"accepted-plan.json",dict(schema="arvm-accepted-effects-v1",tasks=specs,
            authority="trusted harness effect summaries; not inferred permissions from model prose"))
        manifest["backend"]=store.backend.name
        for role in manifest["roles"]:
            workspace=out/role
            workspace.mkdir()
            # An isolated project prevents unrelated repository instructions/tools
            # from contaminating this experiment. Only shared-store tools enabled.
            config=dict(model=a.model,snapshots=False,
                        permissions=[dict(action="*",resource="*",effect="deny"),
                                     dict(action="sharing_*",resource="*",effect="allow")],
                        mcp={"servers":{"sharing":dict(type="local",codemode=False,
                            command=[sys.executable,str(src/"mcp_server.py"),"--store",str(store.root),"--agent",role])}})
            if a.base_url:
                provider,model_id=a.model.split("/",1)
                config["providers"]={provider:dict(name=provider,env=[a.api_key_env],
                    package="@opencode/ai/providers/openai-compatible",settings={"baseURL":a.base_url},
                    models={model_id:dict(name=model_id,capabilities={"tools":True,"input":["text"],"output":["text"]},
                                         limit={"context":16384,"output":2048})})}
            dump(workspace/"opencode.json",config)
            (workspace/"AGENTS.md").write_text("Use only sharing MCP tools for this experiment. Never invoke other agents, shell, network or file tools. Keep every note under 1500 characters. Do not claim a tool succeeded unless it did.\n")
        if a.check_mcp:
            config=json.loads((out/"analyst-0"/"opencode.json").read_text())["mcp"]["servers"]["sharing"]
            # V2's `mcp list` uses the managed service; check in an owned server.
            try:
                result=check_mcp(binary,out/"analyst-0",config,store.root)
            except Exception as exc:
                result={"error":str(exc)}
            dump(out/"mcp-check.json",result)
            methods={json.loads(line)["method"] for path in store.root.glob("mcp-*.jsonl") for line in path.read_text().splitlines()}
            passed="error" not in result and {"initialize","tools/list"}<=methods
            manifest.update(status="mcp-check-passed" if passed else "mcp-check-failed",
                            mcp_methods=sorted(methods))
            return 0 if passed else 2
        # Fail early on unusable model credentials, before launching the pair.
        probe=out/"probe"
        probe.mkdir()
        config=json.loads((out/"analyst-0"/"opencode.json").read_text())
        config.pop("mcp")
        config["permissions"]=[dict(action="*",resource="*",effect="deny")]
        dump(probe/"opencode.json",config)
        manifest["probe"]=invoke(binary,probe,a.model,"Reply exactly OK. Do not use tools.",min(60,a.timeout))
        if not manifest["probe"]["process_ok"]:
            manifest["status"]="blocked-provider"
            return 2
        prompts=[
            f"You are analyst-{i}. Use sharing_read to read evidence and ledger. "
            f"Analyze {(['admission, scope and generation validation','pin lifetime, retirement and reclamation'][i] if a.workload=='runtime-review' else ['retry/dependency','cache/rollback'][i])}. "
            f"Read finding-{i} then use sharing_write with its expected_version to save your evidence-based finding there. "
            f"Use sharing_append to append one line starting analyst-{i}: to ledger. "
            "Reread ledger once to observe any concurrent contribution. Finish with a short confirmation. "
            "Use real tools now; do not merely describe the calls." for i in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            for i in range(2):dsec.admit(f"analyst-{i}")
            futures=[executor.submit(invoke,binary,out/f"analyst-{i}",a.model,prompts[i],a.timeout,store.root) for i in range(2)]
            manifest["analysts"]=[f.result() for f in futures]
        store.trace_kind="verification"
        published=all(store.access(f"finding-{i}")["version"]>0 for i in range(2))
        for result in manifest["analysts"]:dsec.finish(result["agent"],published and result["process_ok"])
        if not published or not all(r["process_ok"] for r in manifest["analysts"]):
            manifest["status"]="incomplete-analysts"
        else:
            prompt="Read evidence, finding-0, finding-1 and ledger using sharing_read. Synthesize the two findings, distinguish verified behavior from hypotheses, and propose one concrete validation test. Read synthesis and publish your result there using sharing_write with expected_version. Keep it below 1500 characters."
            dsec.admit("synthesizer")
            manifest["synthesizer"]=invoke(binary,out/"synthesizer",a.model,prompt,a.timeout,store.root)
            final=store.access("synthesis")
            dump(out/"synthesis.json",final)
            manifest["status"]="complete" if final["version"]>0 and manifest["synthesizer"]["process_ok"] else "incomplete-synthesis"
            dsec.finish("synthesizer",manifest["status"]=="complete")
        events=[json.loads(line) for f in (store.root/"events").glob("*.jsonl") for line in f.read_text().splitlines()]
        metrics=analyze_events(events)
        dump(out/"metrics.json",metrics)
        dsec.snapshot()
        if manifest["status"]=="complete":
            # Validate actual evidence and peer-result consumption, not just a
            # successful CLI exit or existence of a synthesized string.
            good=[e for e in events if e["kind"]=="opencode-live" and e["status"]=="ok"]
            expected={("analyst-0","evidence"),("analyst-1","evidence"),
                      ("synthesizer","finding-0"),("synthesizer","finding-1")}
            observed={(e["agent"],e["object"]) for e in good if e["op"]=="read"}
            if not expected<=observed or metrics["errors"]:
                manifest["status"]="incomplete-tool-coverage"
        for key in store.list_objects():
            if manifest["status"]=="complete":dsec.retire(key)
        dsec.snapshot()
        manifest["validation"]=dict(tool_coverage=manifest["status"]=="complete",
            task_success_definition="required reads, finding publications and synthesis publication; not independent review-quality scoring")
        return 0 if manifest["status"]=="complete" else 2
    finally:
        manifest["finished_utc"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        if manifest["status"]=="running":manifest["status"]="failed"
        dump(out/"manifest.json",manifest)
        if store:store.close()
        if payload.exists():shutil.rmtree(payload)
        print(json.dumps({k:manifest[k] for k in ["status","opencode_version","model"]}),flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
