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
