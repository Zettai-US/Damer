# RUN: python3 -B %S/../tools/benchmark_e2e_speedup.py --workload %S/../examples/qwen27b/qwen27b_workload.json --workload %S/../examples/workloads/moe_alltoall_workload.json --out-dir %T/damer-e2e-speedup-smoke --max-rdma-ops 128 | tee /dev/stderr | FileCheck %s

# CHECK: E2E_SPEEDUP ok=true workloads=2 events=14 bpf_objects=14
# CHECK: E2E_SPEEDUP_REPORT path=
# CHECK: E2E_WORKLOAD name=qwen27b
# CHECK: E2E_WORKLOAD name=moe_alltoall
