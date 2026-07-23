# RUN: python3 -B %S/../tools/damer_offload.py %S/../examples/qwen27b/qwen27b_workload.json --output-dir %T/damer-offload-smoke --no-package | tee /dev/stderr | FileCheck %s

# CHECK: OFFLOAD ok=true cases=9 bpf_objects=9
# CHECK: OFFLOAD_BUNDLE path=
