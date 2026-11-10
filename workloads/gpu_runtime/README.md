# GB10 CUDA runtime port

This is the first executable GPU backend for Damer's accepted object-use
contract. It builds independently of LLVM/CIRCT, Python multiprocessing, NUMA
node IDs, and the x86 CXL SpinLock. The existing CPU experiments are unchanged.

After host setup and **one kernel launch**, `persistent` executes a fixed accepted
DAG entirely on the GPU: readiness checks, slot reservation, pin acquisition,
computation, immutable publication, completion, and reclamation of unused
slots. No host callback, per-task launch, CPU progress thread, or CPU polling
loop is required by this backend. The host blocks on a CUDA event and reads
results after completion. This does **not** mean zero CPU use during process
startup, plan validation, allocation, initial upload, kernel launch, or teardown.

## Implemented scope

- A cooperative grid executes one ready task at a time, parallelizing its
  payload over the resident blocks. Grid size is bounded by measured kernel
  occupancy; no oversubscribed global spin barrier is used.
- Device objects have immutable identity, publication state, remaining accepted
  uses, pins, roots, and a reusable payload slot. Payload access drains at a grid
  barrier before the slot can be reclaimed. Device metadata has one writer.
- Wave ports the periodic two-channel update in `../science_loop/kernels.py`.
  Only the final state is retained; the original trajectory snapshot history,
  surrogate training, and feedback selection are **not** ported yet.
- A non-topologically ordered diamond exercises fanout, joins, future-use
  retention, and reuse. Numerical and lifetime invariants are checked after
  every invocation. Capacity exhaustion and corrupt metadata fail closed;
  nonfinite output is never published.
- Bounded preallocated storage, no migration or capacity expansion. Repetitions
  are independent runs; the GPU releases prior-run output roots only after the
  grid drains. There are no external readers retaining those outputs.

This is not the full DSec authorization or replanning implementation. There is
no concurrent task execution, asynchronous DMA/NIC lifetime tracking, new DAG
submission during execution, SSD I/O, or cross-machine transport. GB10 unified
memory is not evidence of CXL pooling. `cudaMalloc` is used, with no CPU access
to payload or metadata during execution; no managed-memory demand paging is
required by the application.

## Build on GB10 (ARM64)

Use CMake >= 3.24, a CUDA >= 13 toolkit, a supported host compiler, and Python
with NumPy for tests. Run the build **on the GB10**; a desktop x86 executable is
not an ARM64 executable. The default device architecture is `121`.

```bash
python3 workloads/gpu_runtime/preflight.py > gb10-preflight.json
cmake -S workloads/gpu_runtime -B build/gpu-runtime-gb10 -G Ninja \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DCMAKE_CUDA_ARCHITECTURES=121 -DCMAKE_BUILD_TYPE=Release
cmake --build build/gpu-runtime-gb10
ctest --test-dir build/gpu-runtime-gb10 --output-on-failure
build/gpu-runtime-gb10/damer_gpu --mode persistent --n 64 --steps 64 --repeats 20
python3 workloads/gpu_runtime/run_suite.py \
  --binary build/gpu-runtime-gb10/damer_gpu --output results/gb10-first-run
```

Select the installed toolkit's actual path if it differs. The build uses that
compiler's runtime, avoiding accidental linkage to an older system libcudart.
For an RTX 5090 smoke test use a different build directory and
`-DCMAKE_CUDA_ARCHITECTURES=120`. Unsupported cooperative launch fails explicitly.

```bash
compute-sanitizer --tool memcheck --error-exitcode 99 \
  build/gpu-runtime-gb10/damer_gpu --n 33 --steps 64 --repeats 2
compute-sanitizer --tool synccheck --error-exitcode 99 \
  build/gpu-runtime-gb10/damer_gpu --workload diamond --repeats 3
```

`test_runtime.py` compares all three modes bit-for-bit and checks the wave
result against the existing independent NumPy implementation, including odd
grid widths and one-step updates. It also tests bounded allocation, fanout,
cycle rejection, corrupt use counts, NaN, and invalid command-line arguments.

## Local control modes

| Mode | Timed host submissions | Task/lifetime control | Purpose |
|---|---:|---|---|
| `persistent` | One cooperative kernel | GPU | Entire accepted DAG progresses in one kernel |
| `host` | Reset + one kernel per task + finish, per repetition | Same GPU code | Isolate repeated launch overhead |
| `graph` | One CUDA Graph launch | Same GPU code | Strong fixed-workflow control with amortized host submission |

CUDA Graph captures all repetitions before timing. It, too, needs no per-task
host submission during replay. Do not describe the local host mode as NCCL or
claim that only our persistent mode avoids per-operation CPU submission.
Neither control models CPU-side DSec/JSON bookkeeping. Graph construction,
upload, allocations, warmup, and result validation are outside timing; graph
size grows with task count times repetitions.

Each invocation emits JSON with device and CUDA versions, CUDA event elapsed
time, wall time, process CPU time, submission counts, pool size, peak occupancy,
publication/retirement counts, and correctness. Process CPU time includes all
process threads and measurement/submission overhead; it is not proof of zero
system CPU use. No speedup versus a network collective can be inferred from
these single-GPU measurements. `run_suite.py` randomizes paired mode order and
archives commands and source/binary hashes without overwriting prior results.

## NCCL comparison entry point

The optional `damer_nccl` executable runs real NCCL AllReduce with either host
submissions or a captured CUDA Graph. It requires MPI, NCCL headers/libraries,
and at least two ranks with one GPU per local rank. For GB10, use two machines.
It checks every output element and emits per-rank CPU/wall/GPU times. It has no
socket fallback of its own; inspect NCCL logs to identify NCCL's actual transport.

```bash
cmake -S workloads/gpu_runtime -B build/gpu-runtime-gb10 \
  -DDAMER_NCCL=ON
cmake --build build/gpu-runtime-gb10
# Replace both host names and the absolute executable path; place the same
# native ARM64 build and runtime dependencies on both hosts first.
NCCL_DEBUG=INFO mpirun -np 2 --host GB10_A:1,GB10_B:1 \
  -x NCCL_DEBUG /absolute/path/damer_nccl --mode host --count 262144 --repeats 100
NCCL_DEBUG=INFO mpirun -np 2 --host GB10_A:1,GB10_B:1 \
  -x NCCL_DEBUG /absolute/path/damer_nccl --mode graph --count 262144 --repeats 100
```

Use `NCCL_INCLUDE_DIR` and `NCCL_LIBRARY` CMake arguments for non-system installs.
Use an external job timeout, e.g. `timeout 120s mpirun ...`, during bring-up so a
broken network does not hang indefinitely. MPI bootstrap and NCCL setup occur
before the timed region. The program queries advertised device API/GIN
capabilities with NCCL >= 2.29; `-1` means the query is unavailable, not unsupported.
For >= 2.31 it uses the multi-backend capability array; older versions report
only their selected GIN type.

**GIN transfer code is not implemented in this port.** Its output always records
`gin_transfer_tested=false`. Advertised support does not establish a usable
GDAKI path. These AllReduce timings are a transport baseline, not an equivalent
wave/DAG workload, and must not be used to compute an end-to-end Damer speedup.

The comparison must distinguish these cases:

| Path | Per-transaction CPU progress | Host setup | Status here |
|---|---|---|---|
| NCCL host API | Host submission; progress depends on transport | Required | Optional AllReduce executable |
| NCCL + CUDA Graph | Replay amortizes host submissions; transport-dependent progress | Required | Optional AllReduce graph control |
| GIN host-proxy | CPU proxy advances network work | Required | Capability query only |
| GIN GDAKI | GPU can submit and poll network work without a CPU proxy | Required | Capability query only |
| Damer persistent CUDA | GPU advances this fixed local DAG and object lifetimes | Required | Implemented; local tests |

NVIDIA explicitly documents that GDAKI moves WQE preparation, doorbells, and
completion polling to the GPU, with CPU queue creation/registration during
initialization. Therefore **“all GIN transactions require CPU involvement” is
not a supported claim**. The intended future comparison concerns how much of
the surrounding workflow and lifetime protocol remains host-driven; this port
does not establish an advantage over an application that implements the same
protocol on top of NCCL's device API.

Official references, checked 2026-11-10:

- [DGX Spark porting guide](https://docs.nvidia.com/dgx/dgx-spark-porting-guide/latest/porting/software-requirements.html)
- [NCCL device API and backend requirements](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/deviceapi.html)
- [NCCL host setup and GIN capability queries](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/api/device_setup.html)
- [NVIDIA explanation of GDAKI's GPU data path](https://developer.nvidia.com/blog/doca-gpunetio-gda-ki-unified-gpu-networking/)

## Remaining GB10 acceptance work

Local validation on 2026-11-10: RTX 5090 (`sm_120`), CUDA 13.1; all five
integration-test methods and their subcases passed. Compute Sanitizer memcheck
and synccheck reported zero errors. Fifteen randomized local-control trials
passed validation. The `sm_121` device image compiled successfully; this is not
an ARM64 or GB10 runtime test. The optional NCCL 2.32.3 executable compiled and
linked, and rejected a one-rank run as intended. No multi-rank or GIN transfer
was measured. Local evidence is under `build/gpu-runtime/evidence-5090/`.

1. Native ARM64 build, numerical/lifetime tests, sanitizers, and provenance on
   the actual GB10. An x86 build with `sm_121` verifies device compilation only.
2. Two-node network/driver/NCCL inventory, actual transport logs, and AllReduce
   validation on both hosts. Keep host API and CUDA Graph controls separate.
3. Implement and test actual GIN proxy/GDAKI transfers and a matching Damer
   network transport. Match messages, bytes, completion semantics, and work.
4. Port the remaining science-loop stages and dynamic fragment/lifetime
   operations before claiming a complete CPU-independent discovery loop.
