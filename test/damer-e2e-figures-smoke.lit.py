# RUN: python3 -B %S/../tools/benchmark_e2e_speedup.py --workload %S/../examples/qwen27b/qwen27b_workload.json --workload %S/../examples/workloads/moe_alltoall_workload.json --out-dir %T/damer-e2e-figures-smoke --max-rdma-ops 128 > %T/damer-e2e-figures-bench.log
# RUN: python3 -B %S/../tools/plot_e2e_speedup.py --report %T/damer-e2e-figures-smoke/e2e_speedup_report.json --out-dir %T/damer-e2e-figures-smoke/figures | tee /dev/stderr | FileCheck %s

# CHECK: E2E_FIGURES ok=true figures=6
# CHECK: E2E_FIGURE path=
# CHECK: E2E_FIGURES_MANIFEST path=
