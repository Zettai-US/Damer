"""Cross-process CXL-resident mutex using the supplied upstream implementation."""
import ctypes
import errno
import hashlib
import json
import mmap
import os
from pathlib import Path


class CXLBackend:
    name="cxl-spin-fast_exp-55b7072"
    semantics="exclusive-for-read-and-write"

    def __init__(self, root, config):
        from store import hardware
        self.root=Path(root)
        self.directory=Path(config["payload_root"])/".locks"
        self.node=config["lock_node"]
        if not hardware(self.node)["verified_cxl_node"]:
            raise RuntimeError("lock node is not verified physical CXL")
        build=Path(__file__).parent/"build"
        self.provenance=json.loads((build/"provenance.json").read_text())
        library=build/"libdamer_cxl_lock.so"
        if hashlib.sha256(library.read_bytes()).hexdigest()!=self.provenance["library_sha256"]:
            raise RuntimeError("CXL lock library hash mismatch")
        if config.get("lock_library_sha256")!=self.provenance["library_sha256"]:
            raise RuntimeError("store and lock library provenance differ")
        self.lib=ctypes.CDLL(str(library))
        self.lib.cxl_lock_bytes.restype=ctypes.c_size_t
        self.lib.cxl_lock_init.argtypes=[ctypes.c_void_p]
        self.lib.cxl_lock_attach.argtypes=[ctypes.c_void_p]
        self.lib.cxl_lock_attach.restype=ctypes.c_void_p
        self.lib.cxl_lock_acquire.argtypes=[ctypes.c_void_p,ctypes.c_uint64,
                                          ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)]
        self.lib.cxl_lock_release.argtypes=[ctypes.c_void_p]
        self.lib.cxl_lock_detach.argtypes=[ctypes.c_void_p]
        self.maps={}
        self.held=set()

    def create(self,key):
        """Harness only, before publishing the store to any client."""
        from store import address,bind,verify_placement,validate_name
        validate_name(key)
        self.directory.mkdir(exist_ok=True)
        fd=os.open(self.directory/key,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        try:
            os.ftruncate(fd,mmap.PAGESIZE)
            with mmap.mmap(fd,mmap.PAGESIZE) as buf:
                bind(buf,self.node)
                buf[:]=b"\0"*mmap.PAGESIZE
                if self.lib.cxl_lock_bytes()>len(buf):raise RuntimeError("lock exceeds page")
                self.lib.cxl_lock_init(address(buf))
                verify_placement(buf,self.node)
        finally:os.close(fd)

    def _attach(self,key):
        from store import address,bind,verify_placement,validate_name
        validate_name(key)
        if key not in self.maps:
            fd=os.open(self.directory/key,os.O_RDWR)
            try:buf=mmap.mmap(fd,mmap.PAGESIZE)
            finally:os.close(fd)
            bind(buf,self.node)
            _=buf[0]
            nodes=verify_placement(buf,self.node)
            handle=self.lib.cxl_lock_attach(address(buf))
            if not handle:
                buf.close()
                raise MemoryError("CXL lock attach failed")
            self.maps[key]=(buf,handle,nodes)
        return self.maps[key]

    def acquire(self,key,mode,timeout_s):
        if mode not in {"read","write"}:raise ValueError("unknown lock mode")
        if key in self.held:raise RuntimeError("recursive acquisition is unsupported")
        _,handle,nodes=self._attach(key)
        failed,wait=ctypes.c_uint64(),ctypes.c_uint64()
        rc=self.lib.cxl_lock_acquire(handle,int(timeout_s*1e9),ctypes.byref(failed),ctypes.byref(wait))
        if rc==errno.ETIMEDOUT:raise TimeoutError(f"CXL mutex timeout for {key}; no forced unlock")
        if rc:raise OSError(rc,"CXL mutex acquisition failed")
        self.held.add(key)
        return dict(token=key,contended=failed.value>0,failed_attempts=failed.value,
                    native_wait_ns=wait.value,lock_nodes=nodes,lock_semantics=self.semantics)

    def release(self,token):
        if token not in self.held:raise RuntimeError("unlock without ownership")
        self.lib.cxl_lock_release(self.maps[token][1])
        self.held.remove(token)

    def close(self):
        if self.held:raise RuntimeError("cannot close while owning CXL mutex")
        for buf,handle,_ in self.maps.values():
            self.lib.cxl_lock_detach(handle)
            buf.close()
        self.maps.clear()
