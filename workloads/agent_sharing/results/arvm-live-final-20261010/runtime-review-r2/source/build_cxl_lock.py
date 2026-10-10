#!/usr/bin/env python3
"""Build the pinned, unmodified upstream lock plus a small C ABI bridge."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
COMMIT="55b707259c65d79b5f12a2f216b773c2a2a84124"
REPO=ROOT/"third_party/mutex_benchmark"
BUILD=ROOT/"build"


def build():
    if not REPO.exists():
        subprocess.run(["git","clone","--depth","1","--branch","fast_exp",
                        "https://github.com/EIRNf/mutex_benchmark.git",str(REPO)],check=True)
    actual=subprocess.check_output(["git","-C",str(REPO),"rev-parse","HEAD"],text=True).strip()
    if actual!=COMMIT:raise RuntimeError(f"Expected pinned upstream {COMMIT}, found {actual}")
    if subprocess.check_output(["git","-C",str(REPO),"status","--porcelain"],text=True).strip():
        raise RuntimeError("upstream checkout has local modifications")
    BUILD.mkdir(exist_ok=True)
    source=ROOT/"cxl_lock_bridge.cpp"
    argv=["g++","-std=c++20","-O2","-fPIC","-shared","-pthread","-Dcxl",
          "-I"+str(REPO),str(source),"-o",str(BUILD/"libdamer_cxl_lock.so")]
    subprocess.run(argv,check=True)
    files=[source,*[REPO/p for p in ["lib/lock/spin_lock.hpp","lib/lock/lock.hpp",
           "lib/lock/trylock.hpp","lib/utils/region_layout.hpp","lib/utils/cxl_utils.hpp",
           "lib/utils/bench_utils.hpp"]]]
    provenance=dict(upstream_url="https://github.com/EIRNf/mutex_benchmark/tree/fast_exp",
                    upstream_commit=COMMIT,algorithm="upstream SpinLock (region-backed, exclusive)",
                    flags=argv,compiler=subprocess.check_output(["g++","--version"],text=True).splitlines()[0],
                    source_sha256={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
                    library_sha256=hashlib.sha256((BUILD/"libdamer_cxl_lock.so").read_bytes()).hexdigest(),
                    simulated_cxl=False,allocator="caller-supplied MAP_SHARED tmpfs, mbind + resident page verification")
    (BUILD/"provenance.json").write_text(json.dumps(provenance,indent=2)+"\n")
    print(json.dumps(provenance,indent=2))


if __name__=="__main__":build()
