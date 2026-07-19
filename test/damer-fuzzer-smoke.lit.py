# RUN: python3 -B %S/../tools/fuzz_damer_compile.py --iterations 512 --seed 0xD00D --invalid-rate 0.25 --crash-dir %T/damer-fuzzer-crashes | tee /dev/stderr | FileCheck %s

# CHECK: FUZZ iterations=512 ok=true
# CHECK: FUZZ_COVERAGE actions=9 nodes=5
# CHECK: FUZZ_TIMING compile_median_us=
