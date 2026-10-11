"""Observed, versioned arVM memory dataflow; no timing model or inferred ACLs."""
from collections import defaultdict
import json
from pathlib import Path
import statistics

from analyze import analyze_events, percentile
from store import dump


def compile_memory_contract(tasks,object_versions):
    """Lower trusted accepted effects to sharing/retention and DSec scopes.

    A permissions list alone does not identify the same version or final use.
    This pass keeps task order, common backing and pending consumers separate.
    Mutable versions are resolved at tool access, under the CXL object mutex.
    """
    order=[];active=set();ancestors={}
    def visit(name):
        if name in active:raise ValueError("cyclic accepted plan")
        if name in ancestors:return
        if name not in tasks:raise ValueError("unknown task dependency")
        active.add(name);deps=tasks[name].get("depends_on",[]);parents=set(deps)
        for dep in deps:visit(dep);parents.update(ancestors[dep])
        active.remove(name);ancestors[name]=parents;order.append(name)
    for task in tasks:visit(task)
    objects=[];edges=[]
    for key,version in sorted(object_versions.items()):
        readers=[a for a,t in tasks.items() if key in t.get("effects",t["grants"]).get("read",[])]
        writers=[a for a,t in tasks.items() if any(key in t.get("effects",t["grants"]).get(op,[]) for op in ["write","append"])]
        for writer in writers:
            for reader in readers:
                if writer in ancestors[reader]:edges.append(dict(producer=writer,consumer=reader,object=key,type="publication_dependency"))
        concurrent_writers=[[a,b] for i,a in enumerate(writers) for b in writers[i+1:]
                            if a not in ancestors[b] and b not in ancestors[a]]
        objects.append(dict(object=key,alias_set=key,initial_version=version,readers=readers,writers=writers,
            sharing="immutable common backing" if not writers else "synchronized mutable backing",
            required_sync="CXL SpinLock",concurrent_writers=concurrent_writers,
            retention_until_tasks_complete=sorted(set(readers+writers)),
            version_binding="fixed" if not writers else "resolve current version at admitted tool call"))
    known=set(object_versions)
    for task in tasks.values():
        for op,keys in task["grants"].items():
            if op not in {"read","write","append"} or not set(keys)<=known:raise ValueError("unknown effect")
        for op,keys in task.get("effects",task["grants"]).items():
            if not set(keys)<=set(task["grants"].get(op,[])):raise ValueError("effect exceeds scope")
    return dict(schema="arvm-memory-contract-v1",tasks=tasks,admission_order=order,objects=objects,
        data_dependencies=edges,authority="trusted harness effect summaries",reclamation="DSec checks pending tasks and in-flight pins",
        limitations=["Object-granularity effects; no automatic alias analysis for arbitrary pointers",
                     "Scopes are local MCP enforcement, not DPU isolation or transparent memory revocation"])


def interval_union(intervals):
    end=total=0
    for a,b in sorted(intervals):
        total+=max(0,b-max(a,end))
        end=max(end,b)
    return total


def reconstruct(events):
    events=[e for e in events if e["kind"]=="opencode-live"]
    metrics=analyze_events(events)
    good=sorted((e for e in events if e["status"]=="ok"),key=lambda e:e["acquired_ns"])
    publications={}
    uses=defaultdict(list)
    nodes=[]
    edges=[]
    prior_agent=defaultdict(list)
    for e in good:
        nodes.append(dict(id=e["event_id"],kind="operation",agent=e["agent"],op=e["op"],
                          start_ns=e["acquired_ns"],end_ns=e["released_ns"]))
        k=(e["object"],e["version_read"])
        uses[k].append(e)
        if k not in publications:
            if e["producer"]!="harness":raise ValueError("consumer before publication")
            publications[k]=dict(id=f"{k[0]}@{k[1]}",object=k[0],version=k[1],producer="harness",
                published_ns=e["published_ns"],logical_bytes=e["logical_bytes_read"],kind="object_version")
        src=publications[k]
        if src["published_ns"]>e["acquired_ns"]:raise ValueError("read before publish")
        edges.append(dict(source=src["id"],destination=e["event_id"],type="RAW" if e["op"]=="read" else "RMW",
            object=k[0],version=k[1],alias_set=e["backing_id"],byte_range=e["byte_range"],
            bytes=e["logical_bytes_read"],memory_tier=e["memory_tier"],transform="Observe"))
        if "version_written" in e:
            newkey=(e["object"],e["version_written"])
            pub=dict(id=f"{newkey[0]}@{newkey[1]}",object=newkey[0],version=newkey[1],
                     producer=e["agent"],published_ns=e.get("published_written_ns",e["acquired_ns"]),
                     logical_bytes=e["logical_bytes_written"],kind="object_version")
            # The publication timestamp read by consumers is inside the lock;
            # released_ns may be sampled after the next worker acquires it.
            if newkey in publications:raise ValueError("duplicate publication")
            publications[newkey]=pub
            edges.append(dict(source=e["event_id"],destination=pub["id"],type="publish",transform="Persist"))
            edges.append(dict(source=src["id"],destination=pub["id"],type="WAW",alias_set=e["backing_id"]))
            for reader in uses[k]:
                if reader["op"]=="read":
                    edges.append(dict(source=reader["event_id"],destination=e["event_id"],type="WAR",
                                      alias_set=e["backing_id"]))
            # A read may influence a later model-written output. This is a
            # conservative context dependency, not proven semantic influence.
            for reader in prior_agent[e["agent"]]:
                edges.append(dict(source=reader["event_id"],destination=e["event_id"],
                                  type="may_influence",certainty="conservative agent context"))
        if e["op"]=="read":prior_agent[e["agent"]].append(e)
    rows=[]
    for key,pub in sorted(publications.items()):
        accesses=uses[key]
        reads=[e for e in accesses if e["op"]=="read"]
        readers=sorted({e["agent"] for e in reads})
        first=min((e["acquired_ns"] for e in accesses),default=pub["published_ns"])
        last=max((e["released_ns"] for e in accesses),default=first)
        active=interval_union([(e["acquired_ns"],e["released_ns"]) for e in accesses])
        actual_published=accesses[0]["published_ns"] if accesses else pub["published_ns"]
        # A future overwrite ends version validity. Never merge versions just
        # because their object IDs or backing allocation match.
        following=publications.get((key[0],key[1]+1))
        for e in reads:
            if following and e["acquired_ns"]>following["published_ns"]:
                raise ValueError("old-version read after overwrite")
        rows.append(dict(object=key[0],version=key[1],producer=pub["producer"],readers=readers,
            read_count=len(reads),reader_fanout=len(readers),logical_bytes=pub["logical_bytes"],
            published_ns=actual_published,first_access_ns=first,last_access_ns=last,
            observed_span_ns=last-first,active_union_ns=active,
            observed_idle_gap_ns=max(0,last-first-active),
            shared_payload_copy_opportunity_bytes=max(0,len(readers)-1)*pub["logical_bytes"]))
    metrics.update(object_versions=len(rows),shared_versions=sum(r["reader_fanout"]>=2 for r in rows),
        max_reader_fanout=max((r["reader_fanout"] for r in rows),default=0),
        observed_idle_gap_ns=sum(r["observed_idle_gap_ns"] for r in rows),
        same_version_copy_opportunity_bytes=sum(r["shared_payload_copy_opportunity_bytes"] for r in rows))
    return dict(schema="arvm-observed-shared-state-v1",metrics=metrics,versions=rows,
        semantic_movement_graph=dict(nodes=[*nodes,*publications.values()],edges=edges),
        limitations=["Only instrumented MCP shared objects; not all process/KV-cache/LLM memory",
            "RAW/WAR/WAW are observed version dependencies; may_influence is conservative, not semantic proof",
            "Idle gaps are retrospective retention observations, not a prediction of future consumers",
            "Version copy opportunity is logical payload bytes; model prompts/RPC still copy returned text",
            "Mutable versions reuse one backing allocation and are not persistent snapshots"])


def analyze_run(root):
    root=Path(root)
    events=[json.loads(line) for p in (root/"store/events").glob("*.jsonl") for line in p.read_text().splitlines()]
    report=reconstruct(events)
    config=json.loads((root/"store/store.json").read_text())
    good=[e for e in events if e["status"]=="ok"]
    for e in good:
        if e["nodes"]!={str(config["node"]):config["capacity"]//4096}:raise ValueError("payload residency")
        if e["lock_nodes"]!={str(config["lock_node"]):1}:raise ValueError("lock residency")
    life=[json.loads(line) for p in (root/"store").glob("dsec-*.jsonl") for line in p.read_text().splitlines()]
    pins={e["token"]:e for e in life if e["event"]=="pin"}
    unpins={e["token"]:e for e in life if e["event"]=="unpin"}
    if set(pins)!=set(unpins):raise ValueError("undrained or unmatched grants")
    grants=[e["grant_ns"] for e in pins.values()]
    revokes=[e["revoke_ns"] for e in unpins.values()]
    report["grant_path"]=dict(samples=len(grants),grant_p50_us=percentile(grants,.5)/1000 if grants else None,
        grant_p95_us=percentile(grants,.95)/1000 if grants else None,
        release_p50_us=percentile(revokes,.5)/1000 if revokes else None,
        scope="Python/CXL registry per-tool acquire/check/pin; includes cold mapping; not hardware access-control latency")
    manifest=json.loads((root/"manifest.json").read_text())
    report["validation"]=dict(status=manifest["status"],checks=["unique events and committed versions",
        "read-to-producer linkage","version overwrite order","lock and payload resident CXL pages","balanced grant pins"],
        backend=config["lock_provenance"],episode_kind=manifest["kind"])
    dump(root/"arvm-dataflow.json",report)
    return report
