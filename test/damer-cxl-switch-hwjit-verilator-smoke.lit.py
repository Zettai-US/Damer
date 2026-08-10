# RUN: python3 -B %S/../tools/run_cxl_switch_hwjit_sim.py %S/../examples/damer/kv_pack.event.json --out-dir %T/damer-cxl-switch-hwjit-verilator-smoke --clean | tee /dev/stderr | FileCheck %s

# CHECK: HW_JIT_SIM ok=true switchlet=kv_pack
# CHECK: commands=1
# CHECK: HW_JIT_ARTIFACT sv=
# CHECK: HW_JIT_REPORT path=
# CHECK: HW_JIT_SIM ok=true module=damer_cxl_switch_hwjit_kv_pack commands=1 transform_or=1
