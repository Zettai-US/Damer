# Damer eBPF Middleware Compiler

Damer uses bpftime/eBPF as the programmable frontend for data-movement intent.
The middleware compiler turns that intent into a verified semantic movement
plan, chooses where movement-side transforms should run, and emits bpftime-facing
artifacts.

The main path does not require CIRCT:

```text
bpftime/eBPF policy or event JSON
        |
        v
Damer switchlet / movement intent
        |
        v
damer-compile
        |
        +--> semantic movement plan
        +--> bpftime BPF C stub
        +--> bpftime runtime manifest
```

## eBPF Middleware Compiler

Compile the high-level switchlet form:

```mlir
damer.switchlet @kv_pack(
    %src : memref<?xf16, "cxl">,
    %dst : memref<?xi8, "cxl">) {
  %v = damer.read %src
  %q = damer.quantize %v
  damer.write %q, %dst
}
```

Generate an optimized plan:

```bash
tools/damer_compile.py examples/damer/kv_pack.damer --emit plan
```

Generate bpftime-facing eBPF C:

```bash
tools/damer_compile.py examples/damer/kv_pack.event.json --emit bpf-c
```

Generate all middleware artifacts:

```bash
tools/damer_compile.py examples/damer/kv_pack.event.json \
  --emit all \
  --output-dir out/kv_pack
```

The compiler recognizes these fused movement actions:

```text
Move
Move + Quantize
Move + Compress
Move + Checksum
Move + Filter
Move + Reduce
Move + Scatter/Gather
Move + Replicate
Move + Persist
```

It models:

```text
Node: host memory, CXL memory device, GPU memory, accelerator,
      switch compute engine
Edge: bytes, stride, reuse distance, read/write ratio, source/destination,
      ordering requirement, alias set, ownership, transformation
```

The compiler also emits a verifier summary with bounded helper calls, bounded
RDMA fanout, TTL requirements for retry/redirect, no blocking waits, and
epoch-atomic update assumptions.

The eBPF-facing ABI is in `include/damer/bpftime_frontend.h`, with a minimal
example in `examples/bpftime/kv_pack.bpf.c`.

## Data Movement + eBPF Offload

Damer's full offload path uses `dputime`, the bpftime fork checked out at
`third_party/dputime`, as the userspace eBPF runtime layer. Damer owns the data
movement graph, placement, BPF helper ABI, and verifier summary; dputime/bpftime
owns eBPF loading, maps, helpers, and runtime attachment.

Clone or refresh the fork:

```bash
git clone https://github.com/vickiegpt/dputime third_party/dputime
git -C third_party/dputime submodule update --init --recursive
```

Build the dputime/bpftime CLI in a local output directory:

```bash
cmake -S third_party/dputime -B out/dputime-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBPFTIME_ENABLE_LTO=NO \
  -DBPFTIME_ENABLE_UNIT_TESTING=OFF \
  -DBPFTIME_BUILD_WITH_LIBBPF=OFF \
  -DBPFTIME_BUILD_KERNEL_BPF=OFF \
  -DBPFTIME_LLVM_JIT=NO \
  -DENABLE_EBPF_VERIFIER=OFF
CCACHE_DISABLE=1 ninja -C out/dputime-build bpftime
```

The local build smoke should expose:

```bash
out/dputime-build/tools/cli/bpftime --help
```

Build the two observer runtimes used by full fabric offload:

```bash
tools/build_dputime_observers.py \
  --cuda-root /usr/local/cuda-12.8 \
  --remote ubuntu@128.114.53.47 \
  --remote-native-arm \
  --ssh-option=-o \
  --ssh-option=UserKnownHostsFile=/tmp/damer_known_hosts \
  --ssh-option=-o \
  --ssh-option=StrictHostKeyChecking=accept-new
```

This records:

```text
out/dputime-observers/observers.json
```

Add `--cuda-smoke` on a GPU host to run a live CUDA attach smoke. The smoke
builds a tiny `sm_120` CUDA binary with embedded PTX, starts the dputime
syscall-server preload with an eBPF CUDA probe, runs the CUDA client with the
dputime agent preload, and records whether the NV attach path matched a pass,
patched PTX, and loaded the patched module:

```bash
tools/build_dputime_observers.py \
  --cuda-root /usr/local/cuda-12.8 \
  --remote ubuntu@128.114.53.47 \
  --remote-native-arm \
  --cuda-smoke \
  --ssh-option=-o \
  --ssh-option=UserKnownHostsFile=/tmp/damer_known_hosts \
  --ssh-option=-o \
  --ssh-option=StrictHostKeyChecking=accept-new
```

The observer report has two roles:

```text
gpu_host:
  x86_64 dputime with BPFTIME_ENABLE_CUDA_ATTACH=1
  observes GPU/CUDA-side policy hooks and GPU memory movement intent

bluefield_dpu:
  native aarch64 dputime on BlueField Ubuntu
  observes DPU/Arm-side policy hooks, RDMA/NIC placement, and offload bundle state
```

The local GPU host can build the CUDA attach runtime when CUDA headers and
libraries are present. Runtime CUDA attach still requires a working NVIDIA
driver and visible GPU; `nvidia-smi` must work before claiming a live 5090 run.

For the BlueField used in our tests, native Arm build required:

```bash
sudo apt-get install -y cmake ninja-build build-essential \
  libboost-dev libncurses-dev libssl-dev libbpf-dev
```

Build a full offload bundle from a single event, `.damer` switchlet, or workload
JSON:

```bash
tools/damer_offload.py examples/qwen27b/qwen27b_workload.json \
  --clean \
  --output-dir out/damer-offload-qwen27b \
  --observers-report out/dputime-observers/observers.json
```

This produces:

```text
offload.bundle.json
damer-offload-bundle.tar.gz
artifacts/<switchlet>/*.plan.json
artifacts/<switchlet>/*.bpf.c
artifacts/<switchlet>/*.bpf.o
artifacts/<switchlet>/*.bpftime.json
```

The bundle records the dputime commit, the custom Damer helper IDs
(`damer_emit_edge=9001`, `damer_submit_decision=9002`), every eBPF program
section, verifier result, object metadata, and the offload contract:

```text
movement intent -> Damer semantic plan -> cross-device verifier
  -> bpftime BPF C -> clang -target bpf object -> dputime runtime bundle
```

Run the lit smoke for this path:

```bash
llvm-lit -av test --filter damer-offload
```

## Qwen 27B DPU E2E

The Qwen 27B E2E test exercises the complete Damer middleware path for a
serving-style data-movement workload:

```text
Qwen phase JSON
    -> Damer semantic movement plan
    -> bpftime BPF C
    -> bpftime manifest
    -> clang -target bpf object
```

Run the local artifact-level test:

```bash
tools/run_qwen27b_dpu_e2e.py --clean
```

The default workload is `examples/qwen27b/qwen27b_workload.json`. It covers:

```text
Move: logits movement
Move + Quantize: KV packing
Move + Compress: prefill activation spill
Move + Checksum: decode KV fetch
Move + Filter: attention mask movement
Move + Reduce: tensor-parallel logits reduction
Move + Scatter/Gather: tensor shard exchange
Move + Replicate: KV replica refresh
Move + Persist: checkpoint persistence
```

Artifacts and the report are written under:

```text
out/qwen27b-dpu-e2e/
```

To validate the same generated artifacts on a reachable DPU Arm OS:

```bash
tools/run_qwen27b_dpu_e2e.py \
  --clean \
  --dpu-host root@<dpu-ip-or-hostname> \
  --remote-dir /tmp/damer-qwen27b-e2e
```

The remote path requires `ssh`, `scp`, and `clang` on the DPU. The local host
can still detect a BlueField PCIe device without rshim or DPU SSH being active;
in that case the script produces local bpftime/DPU-ready artifacts but does not
claim remote execution.

Run a repeated local benchmark for Damer compiler latency, BPF object compile
latency, artifact size, and BPF instruction count:

```bash
tools/benchmark_qwen27b_damer.py --clean --repeat 10 --warmup 1
```

Benchmark reports are written to:

```text
out/qwen27b-damer-benchmark/qwen27b_damer_benchmark.json
out/qwen27b-damer-benchmark/qwen27b_damer_benchmark.md
```

## Multi-Workload E2E Speedup

Run the multi-workload data-movement benchmark:

```bash
tools/benchmark_e2e_speedup.py --clean
```

By default this covers:

```text
examples/qwen27b/qwen27b_workload.json
examples/qwen27b/qwen27b_dpu_decode_workload.json
examples/qwen27b/qwen27b_long_context_cxl_workload.json
examples/qwen27b/qwen27b_pipeline_parallel_workload.json
examples/qwen27b/qwen27b_lora_adapter_workload.json
examples/qwen27b/qwen27b_scaleout_2gpu_workload.json
examples/qwen27b/qwen27b_scaleout_4gpu_workload.json
examples/qwen27b/qwen27b_scaleout_8gpu_workload.json
examples/qwen27b/qwen27b_scaleout_16gpu_workload.json
examples/qwen27b/qwen27b_scaleout_32gpu_workload.json
examples/workloads/moe_alltoall_workload.json
examples/workloads/prefill_decode_colocation_workload.json
examples/workloads/remote_kv_cache_workload.json
examples/workloads/training_checkpoint_workload.json
```

The benchmark compiles every event into a Damer plan, bpftime-facing BPF C, and
a BPF object. Add `--remote` to recompile those BPF programs on the DPU:

```bash
tools/benchmark_e2e_speedup.py \
  --clean \
  --remote ubuntu@128.114.53.47 \
  --remote-dir /tmp/damer-e2e-speedup \
  --ssh-option=-o \
  --ssh-option=UserKnownHostsFile=/tmp/damer_known_hosts \
  --ssh-option=-o \
  --ssh-option=StrictHostKeyChecking=accept-new
```

The reported speedup is a modeled data-movement E2E speedup, not an application
token-latency measurement. The report records the cost-model assumptions and
the real compilation artifacts:

```text
out/e2e-speedup-benchmark/e2e_speedup_report.json
out/e2e-speedup-benchmark/e2e_speedup_report.md
```

Generate publication/README figures from the report:

```bash
tools/plot_e2e_speedup.py \
  --report out/e2e-speedup-benchmark/e2e_speedup_report.json \
  --out-dir out/e2e-speedup-benchmark/figures
```

This writes SVG and PNG versions of:

```text
e2e_speedup_by_workload
e2e_latency_baseline_vs_damer
e2e_event_speedups
```

Analyze where DPU/GPU copy-path optimization would add E2E impact on top of the
current Damer plan:

```bash
tools/optimize_copy_paths.py \
  --report out/e2e-speedup-benchmark/e2e_speedup_report.json \
  --out-dir out/copy-path-optimization
```

This evaluates `current_damer`, `dpu_inline`, `gpu_pretransform_direct`, and
`gpu_peer_direct` candidates for each compiled event. The report separates large
DPU/GPUDirect copy wins from GPU-source pre-transform wins:

```text
out/copy-path-optimization/copy_path_optimization.json
out/copy-path-optimization/copy_path_optimization.md
out/copy-path-optimization/figures/copy_path_savings_by_event.{svg,png}
```

## Damer Compiler Fuzzer

The fuzzer stresses the bpftime-facing middleware compiler with deterministic
JSON and DSL switchlets. It generates valid movement plans plus malformed inputs
that should be rejected cleanly by `CompileError` or the effect verifier.

Run the CI-style smoke test through lit:

```bash
llvm-lit -av test --filter damer-fuzzer
```

Run a longer local fuzz campaign:

```bash
tools/fuzz_damer_compile.py \
  --iterations 10000 \
  --seed 0xD00D \
  --invalid-rate 0.25
```

On an unexpected compiler exception, the fuzzer exits non-zero and writes a
reproducer under:

```text
out/damer-fuzzer-crashes/
```

## Optional MLIR/CXL Passes

The repository also contains an out-of-tree pass scaffold for co-analyzing CXL
data movement in software MLIR and CIRCT hardware IR. This is not the primary
eBPF middleware path.

It contains two metadata-only passes:

- `--cxl-sw-data-movement`: walks `builtin.module`, finds `memref` values in a
  configurable CXL memory space, and annotates loads, stores, copies,
  allocations, function arguments, and generic CXL users.
- `--cxl-hw-data-movement`: walks CIRCT `hw.module` operations, classifies
  host/CXL/device-facing ports and datapath operations, and annotates CXL-facing
  hardware movement boundaries.
The MLIR passes use a shared annotation:

```mlir
{cxl.data_movement = {
  domain = "software" | "hardware",
  kind = "...",
  source = "...",
  destination = "...",
  static_bytes = ...
}}
```

## MLIR Build

Build CIRCT first, then point this project at the generated LLVM, MLIR, and
CIRCT CMake package directories:

```bash
cmake -G Ninja -S . -B build \
  -DLLVM_DIR=/path/to/circt/llvm/build/lib/cmake/llvm \
  -DMLIR_DIR=/path/to/circt/llvm/build/lib/cmake/mlir \
  -DCIRCT_DIR=/path/to/circt/build/lib/cmake/circt
ninja -C build cxl-data-movement-opt
```

## MLIR Software Example

```bash
build/bin/cxl-data-movement-opt \
  --cxl-sw-data-movement \
  test/cxl-sw-data-movement.mlir
```

By default, `memref<..., "cxl">` is considered CXL memory. Integer memory
spaces can be enabled with `--cxl-space-id=<n>`.

## MLIR Hardware Example

```bash
build/bin/cxl-data-movement-opt \
  --cxl-hw-data-movement \
  test/cxl-hw-data-movement.mlir
```

The hardware pass uses these conventions on `hw.module`:

```mlir
attributes {
  cxl.hw.input_roles = ["host", "device"],
  cxl.hw.output_roles = ["device", "host"]
}
```

Per-operation attributes such as `{cxl.hw.role = "device"}` override propagated
roles and mark a CXL-facing endpoint or boundary.

## Concordia Workload Run

The `workloads/concordia` directory contains a local model of the Concordia
SlugArch CXL GEMM workload and a runner that compares this pass against
Concordia's `slugarch run-cxl` path:

```bash
tools/run_concordia_workloads.py --markdown
```

It runs the software pass over the 49-FLIT request/response software model, the
hardware pass over the SlugCXL endpoint boundary model, and the already-built
Concordia `slugarch` binary against the copied GEMM fixture. It also regenerates
descriptor-derived MLIR for Concordia's tracked pipeline rtlmaps, including
`generic_gemm`, `ternary_matmul`, `qwen_decode_token`, and
`qwen_prefill_gemm`:

```bash
tools/generate_concordia_mlir.py
```

The PTXSpatial trace path lowers Concordia PTX or RTL mapping descriptors into a
JSON event trace plus CIRCT HW trace MLIR:

```bash
tools/compile_ptx_to_circt_trace.py --from-concordia
build/tools/cxl-data-movement-opt/cxl-data-movement-opt \
  --cxl-hw-data-movement \
  workloads/concordia/ptxspatial/gemm.circt-trace.mlir
```

By default this reads Concordia's `tests/fixtures/gemm.ptx` from git `HEAD`,
maps the GEMM tensor event to `systolic_array_16x16`, and emits a descriptor
derived trace for each covered pipeline rtlmap.

To see which Concordia inputs are covered and which are still frontier work:

```bash
tools/concordia_coverage_report.py
```

## In-Tree CIRCT Port

To upstream this into CIRCT, move the TableGen declarations into the appropriate
`include/circt/Dialect/*/Passes.td`, move the C++ implementation under
`lib/Dialect/*/Transforms`, add the source to that directory's `CMakeLists.txt`,
and add the test files under `test/Dialect/HW` or a new CXL-focused test
directory. CIRCT's own `circt-opt` will expose the passes once they are included
in the relevant pass registration header.
