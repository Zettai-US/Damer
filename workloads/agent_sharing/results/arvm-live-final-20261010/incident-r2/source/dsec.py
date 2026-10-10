"""DSec: a local data-sharing execution controller for the experiment.

This is our implementation, not DeepSeek's production DSec. It combines scoped
objects, dependency admission, in-flight pins, retention, and bwrap sandboxes.
Every shared control/data critical section uses the upstream CXL mutex.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from store import Store, dump, validate_name


class DSec:
    def __init__(self, store):
        self.store=store
        self.path=store.root/"dsec.json"

    @classmethod
    def initialize(cls, store, specs):
        if (store.root/"dsec.json").exists():raise FileExistsError("DSec already initialized")
        objects=store.list_objects()
        for agent,spec in specs.items():
            validate_name(agent)
            if not set(spec.get("depends_on",[]))<=set(specs):raise ValueError("unknown task dependency")
            for access,keys in spec["grants"].items():
                if access not in {"read","write","append"} or not set(keys)<=set(objects):
                    raise ValueError("invalid object grants")
        # Reject cyclic admission graphs before publishing them.
        visited,active=set(),set()
        def visit(agent):
            if agent in active:raise ValueError("cyclic sandbox dependencies")
            if agent in visited:return
            active.add(agent)
            for dep in specs[agent].get("depends_on",[]):visit(dep)
            active.remove(agent);visited.add(agent)
        for agent in specs:visit(agent)
        state=dict(schema=2,sandboxes={a:dict(**s,state="ready",generation=0) for a,s in specs.items()},
                   pins={},retired=[],created_ns=time.monotonic_ns(),state_location="CXL shared memory")
        store.record("dsec-registry",initial=state,capacity=262144)
        dump(store.root/"dsec.json",state)
        return cls(store)

    def event(self,event,**fields):
        row=dict(event=event,ns=time.monotonic_ns(),pid=os.getpid(),**fields)
        with (self.store.root/f"dsec-{os.getpid()}.jsonl").open("a") as f:f.write(json.dumps(row)+"\n")

    @contextmanager
    def registry(self):
        grant=self.store.backend.acquire("dsec-control","write",10)
        try:
            state=self.store.read_record("dsec-registry")
            yield state
            self.store.write_record("dsec-registry",state)
        finally:self.store.backend.release(grant["token"])

    def admit(self,agent):
        with self.registry() as state:
            task=state["sandboxes"][agent]
            if task["state"]!="ready":raise RuntimeError("sandbox is not ready")
            if any(state["sandboxes"][d]["state"]!="completed" for d in task.get("depends_on",[])):
                raise RuntimeError("sandbox dependency is not completed")
            task.update(state="running",started_ns=time.monotonic_ns())
        self.event("admit",agent=agent)

    def finish(self,agent,success):
        with self.registry() as state:
            task=state["sandboxes"][agent]
            if task["state"]!="running":raise RuntimeError("sandbox is not running")
            active=[p for p in state["pins"].values() if p["agent"]==agent]
            if active:raise RuntimeError("cannot finish sandbox with in-flight object pins")
            task.update(state="completed" if success else "failed",ended_ns=time.monotonic_ns())
        self.event("finish",agent=agent,success=success)

    @contextmanager
    def pin(self,agent,key,operation):
        begin=time.monotonic_ns()
        token=uuid.uuid4().hex
        with self.registry() as state:
            task=state["sandboxes"].get(agent)
            if task is None or task["state"]!="running":raise PermissionError("sandbox not admitted")
            if key in state["retired"] or key not in task["grants"].get(operation,[]):
                raise PermissionError(f"sandbox {agent} has no {operation} grant for {key}")
            state["pins"][token]=dict(agent=agent,object=key,op=operation,started_ns=time.monotonic_ns())
        self.event("pin",agent=agent,object=key,op=operation,token=token,
                   grant_ns=time.monotonic_ns()-begin,generation=task["generation"])
        try:yield
        finally:
            begin=time.monotonic_ns()
            with self.registry() as state:
                if state["pins"].pop(token,None) is None:raise RuntimeError("missing object pin")
            self.event("unpin",agent=agent,object=key,token=token,revoke_ns=time.monotonic_ns()-begin)

    def grant(self,agent,key,operations):
        begin=time.monotonic_ns()
        if not set(operations)<={"read","write","append"} or not operations:
            raise ValueError("invalid grant operations")
        if key not in Store.list_objects(self.store):raise KeyError(key)
        with self.registry() as state:
            task=state["sandboxes"][agent]
            if task["state"] not in {"ready","running"} or key in state["retired"]:
                raise PermissionError("grant target unavailable")
            for op in operations:
                task["grants"][op]=sorted(set(task["grants"].get(op,[]))|{key})
            task["generation"]+=1
        self.event("grant",agent=agent,object=key,operations=operations,
                   generation=task["generation"],duration_ns=time.monotonic_ns()-begin)

    def revoke(self,agent,key=None):
        """Block new tool grants; existing calls drain before finish/reclaim.

        Does not revoke arbitrary CPU pointers or promise DPU isolation.
        """
        begin=time.monotonic_ns()
        with self.registry() as state:
            task=state["sandboxes"][agent]
            task["grants"]={op:[obj for obj in ids if key is not None and obj!=key]
                            for op,ids in task["grants"].items()}
            task["generation"]+=1
        self.event("revoke",agent=agent,object=key,generation=task["generation"],duration_ns=time.monotonic_ns()-begin)

    def snapshot(self):
        with self.registry() as state:dump(self.path,state)

    def retire(self,key):
        with self.registry() as state:
            if any(p["object"]==key for p in state["pins"].values()):raise RuntimeError("object still pinned")
            for task in state["sandboxes"].values():
                if task["state"] in {"ready","running"} and any(key in ids for ids in task["grants"].values()):
                    raise RuntimeError("object retained for a scheduled consumer")
            if key not in state["retired"]:state["retired"].append(key)
        self.event("retire",object=key)


class SandboxStore(Store):
    def __init__(self,root,agent,trace_kind="opencode-live"):
        super().__init__(root,agent,trace_kind)
        self.dsec=DSec(self)

    def list_objects(self):
        with self.dsec.registry() as state:
            task=state["sandboxes"].get(self.agent)
            if task is None or task["state"]!="running":raise PermissionError("sandbox not admitted")
            return sorted(set(sum(task["grants"].values(),[]))-set(state["retired"]))

    def access(self,key,op="read",**kwargs):
        with self.dsec.pin(self.agent,key,op):return super().access(key,op,**kwargs)


def sandbox_command(argv,workspace,store_root=None):
    """Mount only the runtime, this workspace, and its experiment's object store.

    Network stays in the host namespace to reach the loopback model server.
    All capabilities are dropped; other home directories/repositories are absent.
    """
    bwrap=shutil.which("bwrap")
    if not bwrap:raise RuntimeError("bwrap required; no unisolated fallback")
    workspace=Path(workspace).resolve()
    runtime=Path(__file__).resolve().parent
    state=workspace/".dsec-state"
    state.mkdir(exist_ok=True)
    # Preserve absolute paths in OpenCode configs and Python trace provenance.
    cmd=[bwrap,"--unshare-user","--unshare-pid","--unshare-uts","--unshare-ipc",
         "--die-with-parent","--new-session","--cap-drop","ALL",
         "--ro-bind","/usr","/usr","--ro-bind","/lib","/lib","--ro-bind","/lib64","/lib64",
         "--symlink","usr/bin","/bin","--symlink","usr/sbin","/sbin",
         "--proc","/proc","--dev","/dev","--tmpfs","/tmp","--ro-bind","/sys","/sys",
         "--dir","/root","--ro-bind",str(runtime),str(runtime),
         # Hide all historical experiment data and the embedded git checkout.
         "--tmpfs",str(runtime/"results"),"--tmpfs",str(runtime/"third_party"),
         "--bind",str(workspace),str(workspace)]
    for path in ["/etc/ld.so.cache","/etc/hosts","/etc/resolv.conf","/etc/nsswitch.conf","/etc/passwd","/etc/ssl"]:
        if Path(path).exists():cmd.extend(["--ro-bind",path,path])
    executable=Path(argv[0]).resolve()
    if not str(executable).startswith("/usr/"):cmd.extend(["--ro-bind",str(executable),str(executable)])
    if store_root:
        root=Path(store_root).resolve()
        config=json.loads((root/"store.json").read_text())
        cmd.extend(["--bind",str(root),str(root),"--bind",config["payload_root"],config["payload_root"]])
    cmd.extend(["--setenv","XDG_CONFIG_HOME",str(state/"config"),
                "--setenv","XDG_DATA_HOME",str(state/"data"),
                "--setenv","XDG_CACHE_HOME",str(state/"cache"),
                "--setenv","XDG_STATE_HOME",str(state/"state"),
                "--chdir",str(workspace),"--",*argv])
    return cmd
