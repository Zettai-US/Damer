# RUN: python3 -B %S/../tools/benchmark_e2e_speedup.py --workload %S/../examples/qwen27b/qwen27b_workload.json --workload %S/../examples/workloads/moe_alltoall_workload.json --out-dir %T/damer-copy-path-speedup-smoke --max-rdma-ops 128 > %T/damer-copy-path-speedup-smoke.out
# RUN: python3 -B %S/../tools/optimize_copy_paths.py --report %T/damer-copy-path-speedup-smoke/e2e_speedup_report.json --out-dir %T/damer-copy-path-optimization-smoke --no-figures | tee /dev/stderr | FileCheck %s

# CHECK: COPY_PATH_OPT ok=true workloads=2 events=14
# CHECK: COPY_PATH_OPT_REPORT path=
# CHECK: COPY_PATH_OPT_MARKDOWN path=
# CHECK: COPY_PATH_WORKLOAD name=qwen27b
# CHECK: COPY_PATH_WORKLOAD name=moe_alltoall
# CHECK: COPY_PATH_TOP switchlet=
