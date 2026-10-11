"""Instrumented, single-host shared-memory objects for agent experiments.

Only the pinned upstream CXL SpinLock backend is supported. Lock state is
always resident on a verified physical CXL node. No OS file-lock fallback.
Payloads live in tmpfs, bound before first touch; metadata and traces are separate.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import time
import uuid

PAGE = mmap.PAGESIZE
NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def dump(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def validate_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("IDs must match [A-Za-z0-9_-]{1,80}")
    return value


def address(buf):
    return ctypes.addressof(ctypes.c_char.from_buffer(buf))


def bind(buf, node):
    lib = ctypes.CDLL(ctypes.util.find_library("numa"), use_errno=True)
    lib.mbind.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong, ctypes.c_uint]
    lib.mbind.restype = ctypes.c_int
    if node < 0 or node >= ctypes.sizeof(ctypes.c_ulong) * 8:
        raise ValueError("node outside supported nodemask")
    mask = ctypes.c_ulong(1 << node)
    if lib.mbind(address(buf), len(buf), 2, ctypes.byref(mask), 8 * ctypes.sizeof(mask), 0):
        raise OSError(ctypes.get_errno(), "mbind failed; refusing silent fallback")


def placement(buf):
    # Each object is a separate file VMA. numa_maps reports resident pages, not
    # requested policy. Touch all pages before calling this function.
    prefix = f"{address(buf):x} "
    for line in Path("/proc/self/numa_maps").read_text().splitlines():
        if line.startswith(prefix):
            return {int(n): int(count) for n, count in re.findall(r"\bN(\d+)=(\d+)", line)}
    raise RuntimeError("object mapping absent from numa_maps")


def verify_placement(buf, node):
    nodes = placement(buf)
    if nodes != {node: len(buf) // PAGE}:
        raise RuntimeError(f"placement mismatch: {nodes}; expected node {node}, {len(buf)//PAGE} pages")
    return nodes


def hardware(node):
    base = Path(f"/sys/devices/system/node/node{node}")
    blocks = sorted(int(p.name[6:]) for p in base.glob("memory[0-9]*"))
    block_bytes = int(Path("/sys/devices/system/memory/block_size_bytes").read_text(), 16)
    decoders = []
    for p in Path("/sys/bus/cxl/devices").glob("decoder*"):
        if (p / "start").exists() and (p / "size").exists():
            decoders.append(dict(name=p.name, start=int((p/"start").read_text(), 0),
                                 size=int((p/"size").read_text(), 0)))
    covered = bool(blocks) and all(any(d["start"] <= b * block_bytes and
                (b + 1) * block_bytes <= d["start"] + d["size"] for d in decoders) for b in blocks)
    return dict(node=node, node_cpus=(base/"cpulist").read_text().strip(),
                memory_blocks=blocks, block_bytes=block_bytes, cxl_decoders=decoders,
                verified_cxl_node=covered, kernel=os.uname().release)


class Store:
    def __init__(self, root, agent="harness", trace_kind="controlled"):
        self.root = Path(root).resolve()
        self.config = json.loads((self.root / "store.json").read_text())
        self.agent = validate_name(agent)
        self.trace_kind = trace_kind
        self.maps = {}
        self.records = {}
        from cxl_lock import CXLBackend
        self.backend = CXLBackend(self.root, self.config)
        if not getattr(self.backend, "name", ""):
            raise ValueError("lock backend requires an explicit name")

    @classmethod
    def create(cls, root, payload_root, node=1, capacity=1024*1024,
               granularity="object", lock_node=1, require_cxl=True):
        root, payload_root = Path(root).resolve(), Path(payload_root).resolve()
        if capacity <= 0 or capacity % PAGE:
            raise ValueError("capacity must be a positive page multiple")
        if granularity not in {"object", "global"}:
            raise ValueError("invalid lock granularity")
        hw = hardware(node)
        if require_cxl and not hw["verified_cxl_node"]:
            raise RuntimeError("NUMA node is not verified against CXL decoder ranges")
        provenance=json.loads((Path(__file__).parent/"build/provenance.json").read_text())
        root.mkdir(parents=True, exist_ok=False)
        payload_root.mkdir(parents=True, exist_ok=False)
        for name in ["locks", "meta", "events"]:
            (root / name).mkdir()
        config = dict(schema=1, payload_root=str(payload_root), node=node,
                      capacity=capacity, granularity=granularity, hardware=hw,
                      lock_node=lock_node,lock_semantics="exclusive-for-read-and-write",
                      lock_library_sha256=provenance["library_sha256"],lock_provenance=provenance)
        dump(root / "store.json", config)
        store=cls(root)
        store.backend.create("all")
        store.backend.create("dsec-control")
        (payload_root/".records").mkdir()
        return store

    def record(self,key,initial=None,capacity=65536):
        """CXL-resident control record. Caller holds the matching CXL mutex.

        JSON is a prototype representation, not a native capability table.
        Disk JSON files are audit snapshots, never the live source of truth.
        """
        validate_name(key)
        if key not in self.records:
            path=Path(self.config["payload_root"])/".records"/key
            flags=os.O_RDWR | (os.O_CREAT|os.O_EXCL if initial is not None else 0)
            fd=os.open(path,flags,0o600)
            try:
                if initial is not None:os.ftruncate(fd,capacity)
                buf=mmap.mmap(fd,os.fstat(fd).st_size)
            finally:os.close(fd)
            bind(buf,self.config["lock_node"])
            if initial is not None:buf[:]=b"\0"*len(buf)
            else:
                for offset in range(0,len(buf),PAGE):_=buf[offset]
            verify_placement(buf,self.config["lock_node"])
            self.records[key]=buf
            if initial is not None:self.write_record(key,initial)
        return self.records[key]

    def read_record(self,key):
        buf=self.record(key)
        size=int.from_bytes(buf[:8],"little")
        if not 0<size<=len(buf)-8:raise RuntimeError("invalid shared control record")
        return json.loads(buf[8:8+size])

    def write_record(self,key,value):
        buf=self.records[key]
        encoded=json.dumps(value,separators=(",",":")).encode()
        if len(encoded)>len(buf)-8:raise ValueError("shared control record capacity exceeded")
        buf[8:8+len(encoded)]=encoded
        buf[:8]=len(encoded).to_bytes(8,"little")

    def seed(self, key, text):
        """Harness-only initialization, before any workers start."""
        key = validate_name(key)
        data = text.encode()
        capacity = self.config["capacity"]
        if len(data) > capacity:
            raise ValueError("seed exceeds capacity")
        if key in {"all","dsec-control"}:raise ValueError("reserved object ID")
        self.backend.create(key)
        path = Path(self.config["payload_root"]) / key
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, capacity)
            with mmap.mmap(fd, capacity) as buf:
                bind(buf, self.config["node"])
                buf.madvise(mmap.MADV_NOHUGEPAGE)
                buf[:] = b"\0" * capacity
                buf[:len(data)] = data
                nodes = verify_placement(buf, self.config["node"])
                digest = hashlib.sha256(buf).hexdigest()
        finally:
            os.close(fd)
        meta=dict(version=0, size=len(data),sha256=digest, producer="harness",
                  published_ns=time.monotonic_ns(),nodes=nodes)
        self.record("meta-"+key,initial=meta,capacity=PAGE)
        dump(self.root / "meta" / (key + ".json"),meta)

    def close(self):
        for buf in self.maps.values():
            buf.close()
        self.maps.clear()
        for buf in self.records.values():buf.close()
        self.records.clear()
        self.backend.close()

    def _map(self, key):
        if key not in self.maps:
            fd = os.open(Path(self.config["payload_root"]) / key, os.O_RDWR)
            try:
                buf = mmap.mmap(fd, self.config["capacity"])
            finally:
                os.close(fd)
            # Bind before faults in this mapping too; do not move existing pages.
            bind(buf, self.config["node"])
            self.maps[key] = buf
        return self.maps[key]

    def list_objects(self):
        return sorted(p.stem for p in (self.root / "meta").glob("*.json"))

    def reclaim_payload(self,key):
        """Harness allocator: invoke only after DSec retirement and map drain."""
        key=validate_name(key)
        grant=self.backend.acquire(key if self.config["granularity"]=="object" else "all","write",10)
        try:
            path=Path(self.config["payload_root"])/key
            released=path.stat().st_size
            if key in self.maps:self.maps.pop(key).close()
            record="meta-"+key
            if record in self.records:self.records.pop(record).close()
            path.unlink()
            (Path(self.config["payload_root"])/".records"/record).unlink()
            # Keep immutable disk snapshots and lock pages for audit. Names
            # cannot be recycled while the experiment store remains alive.
            return released
        finally:self.backend.release(grant["token"])

    def access(self, key, op="read", text="", expected_version=None,
               return_limit=4096, timeout_s=10, copy_node=None, session_id=None,query=None):
        """read, CAS write, or atomic append. No lock is held during LLM thinking."""
        key = validate_name(key)
        if op not in {"read", "write", "append"}:
            raise ValueError("unknown operation")
        if op == "write" and (not isinstance(expected_version, int) or isinstance(expected_version, bool)):
            raise ValueError("write requires integer expected_version")
        if not isinstance(return_limit, int) or not 0 <= return_limit <= 65536:
            raise ValueError("return_limit outside 0..65536")
        if not 0 < timeout_s <= 60:
            raise ValueError("timeout outside 0..60s")
        if query is not None and (op!="read" or not isinstance(query,str) or not 1<=len(query)<=100):
            raise ValueError("query must be 1..100 characters for a read")
        e = dict(schema=1, event_id=uuid.uuid4().hex, agent=self.agent, pid=os.getpid(), session_id=session_id,
                 actor=getattr(self,"actor",self.agent),phase=getattr(self,"phase","single"),
                 kind=self.trace_kind, object=key, op=op, backend=self.backend.name,
                 granularity=self.config["granularity"], request_ns=time.monotonic_ns(),
                 read_bytes=0, write_bytes=0, copy_bytes=0, response_bytes=0,
                 status="error", acquired_ns=None, released_ns=None, contended=None)
        token = None
        try:
            lock_key = "all" if self.config["granularity"] == "global" else key
            grant = self.backend.acquire(lock_key, "read" if op == "read" else "write", timeout_s)
            token = grant["token"]
            if token is None:
                raise RuntimeError("lock adapter returned a None token")
            e.update(acquired_ns=time.monotonic_ns(), contended=grant.get("contended"))
            e.update({k:grant[k] for k in ["failed_attempts","native_wait_ns","lock_nodes","lock_semantics"]})
            e["wait_ns"] = e["acquired_ns"] - e["request_ns"]
            if e["contended"] is not None and not isinstance(e["contended"], bool):
                raise RuntimeError("lock adapter contended must be bool or None")
            meta_path = self.root / "meta" / (key + ".json")
            meta = self.read_record("meta-"+key)
            buf = self._map(key)
            data_view = buf
            private = None
            try:
                if copy_node is not None:
                    private = mmap.mmap(-1, len(buf), flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
                    bind(private, copy_node)
                    private[:] = memoryview(buf)
                    e["private_nodes"] = verify_placement(private, copy_node)
                    e["copy_bytes"] = len(buf)
                    data_view = private
                digest = hashlib.sha256(data_view).hexdigest()
                e["nodes"] = verify_placement(buf, self.config["node"])
                if digest != meta["sha256"]:
                    raise RuntimeError("payload checksum mismatch (possible interrupted publication)")
                e.update(read_bytes=len(buf), version_read=meta["version"],
                         producer=meta["producer"], published_ns=meta["published_ns"],
                         logical_bytes_read=meta["size"],backing_id=key,byte_range=[0,len(buf)],
                         memory_tier="cxl" if self.config["hardware"]["verified_cxl_node"] else "dram")
                stat=(Path(self.config["payload_root"])/key).stat()
                e.update(backing_inode=stat.st_ino,backing_device=stat.st_dev,allocated_bytes=stat.st_blocks*512,
                         mapping_address=address(buf),payload_capacity=len(buf))
                if op == "read":
                    content=data_view[:min(meta["size"],return_limit)].decode(errors="replace")
                    if query is not None:
                        lines=data_view[:meta["size"]].decode(errors="replace").splitlines()
                        hits=[i for i,line in enumerate(lines) if query.lower() in line.lower()]
                        selected=sorted({j for i in hits for j in range(max(0,i-2),min(len(lines),i+9))})
                        content="\n".join(f"{i+1}: {lines[i]}" for i in selected)[:return_limit]
                        e.update(query=query,matching_lines=len(hits),access_kind="search")
                    result = dict(object=key, version=meta["version"], producer=meta["producer"],
                                  text=content,
                                  logical_bytes=meta["size"], truncated=meta["size"] > return_limit)
                elif expected_version is not None and expected_version != meta["version"]:
                    e["status"] = "conflict"
                    result = dict(error="version_conflict", current_version=meta["version"])
                else:
                    data = (bytes(buf[:meta["size"]]) if op == "append" else b"") + text.encode()
                    if len(data) > len(buf):
                        raise ValueError("object capacity exceeded")
                    buf[:len(data)] = data
                    # Clear the old logical tail without rewriting the padding.
                    if len(data) < meta["size"]:
                        buf[len(data):meta["size"]] = b"\0" * (meta["size"] - len(data))
                    new = dict(version=meta["version"]+1, size=len(data),
                               sha256=hashlib.sha256(buf).hexdigest(), producer=self.agent,
                               published_ns=time.monotonic_ns(), nodes=e["nodes"])
                    self.write_record("meta-"+key,new)
                    dump(meta_path, new)
                    e.update(version_written=new["version"],
                             write_bytes=max(len(data), meta["size"]),logical_bytes_written=len(data),
                             published_written_ns=new["published_ns"])
                    result = dict(object=key, version=new["version"], logical_bytes=len(data))
            finally:
                if private is not None:
                    private.close()
            if e["status"] == "error":
                e["status"] = "ok"
            e["response_bytes"] = len(json.dumps(result).encode())
            return result
        except Exception as exc:
            e["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            release_error = None
            if token is not None:
                try:
                    self.backend.release(token)
                except Exception as exc:
                    release_error = exc
                    e.update(status="error", error="lock release failed: " + str(exc))
                finally:
                    e["released_ns"] = time.monotonic_ns()
                    e["hold_ns"] = e["released_ns"] - e["acquired_ns"]
            e["end_ns"] = time.monotonic_ns()
            # One append syscall per record, separate file for each OS process.
            path = self.root / "events" / f"{self.agent}-{os.getpid()}.jsonl"
            with path.open("a") as f:
                f.write(json.dumps(e) + "\n")
            if release_error is not None:
                raise release_error
