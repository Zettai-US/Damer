arVM executable prototype and measured CXL experiments
======================================================

Measured platform (2026-10-06): AMD Threadripper 7960X, Linux 7.0.0-mk1+,
Montage 128 GiB CXL device 1b00:c002, DRAM node 0 and CXL node 1. Hardware
inventory verifies that all node-1 memory blocks fall in the CXL decoder range.
These experiments use real CXL memory, not injected delays or an emulator.

Build paper and plots from the saved observations: make
Validate the saved evidence: python3 experiments/check_results.py
Run control-path correctness tests: make controls
Rerun native measurements and compiler timings: make bench
Recreate measured figures: make plots
Check the paper's OSDI page budget and typography: make check

Native reproduction requires Linux, GCC, CXL at node 1, DRAM at node 0, the
specified CPU affinity IDs, and permission to call mbind. The test is platform
specific. The runner leaves a 48 MiB free-memory margin on node 0 and limits
private payloads to 32 MiB. It neither drops system caches nor changes device,
swap, CPU-frequency, or scheduler configuration. Only owned buffers are flushed.
Read the source before adapting node IDs or CPU IDs to a different machine.

Files
-----
cxl_bench.c: native read-only memfd publication, NUMA binding and verification,
  multi-process fanout, fresh/reused copying, checksums, PSS sampling, phase replay.
runtime.py: executable registry, epochs, generations, scopes, pins, retirement,
  output reservations, full DAG validation, and incremental output-bound updates.
test_runtime.py: 13 tests, including 100 retirement/admission races and 20,000
  randomized admit/complete events. This is not a proof of OS isolation.
run_native.py: randomized configuration/policy order, five fanout blocks and
  four phase blocks, two warmups per invocation, raw JSONL and source manifest.
bench_compiler.py: 100 trials for each of five graph sizes, timed separately.
summarize.py: summaries and grayscale PDF/PNG figures from recorded observations.
check_results.py: provenance, sample counts, placement, checksums and accounting.
results/: raw observations, summaries, hardware inventory and correctness log.

Timing and scope
----------------
Fanout time begins at parent dispatch and ends at the latest worker completion.
It includes mapping, fresh allocation when selected, copying and scans. Startup,
source creation/flush, result verification, PSS measurement and teardown are
outside the timer. Source pages are flushed once per iteration; repeated scans
retain natural cache reuse. The retained-buffer copy baseline is prefaulted.

PSS is sampled for the payload mappings, not whole processes. For shared access,
include publisher and worker mappings. For copying, reported private-buffer PSS
excludes the separately retained CXL source, which both policies keep.

Phase time includes the configured planning interval, staging and consumption.
It uses one prefaulted DRAM slot, four CXL objects, and a 20-phase cyclic trace.
The source pool is flushed once per episode. Wrong hints cause a corrective copy
before scanning, and every checksum is checked. Planning gaps and accuracies are
controlled inputs, not observations from an LLM. Compiler overhead is measured
separately and is not included in phase timings. Demand copying is not MemTrade.

The controller and native backend are component prototypes, not an integrated
production agent runtime. Opaque tool handling, live LLM plans, multi-session
contention, leased-memory recall, and full crash recovery remain unmeasured.

Plot map
--------
measured_fanout.pdf: 4 MiB, one scan, 1/4/8 workers, three policies; bars show
25-observation medians, whiskers the range of five block medians.
measured_phases.pdf: 32 MiB, three hint accuracies and four planning intervals;
12-observation median episode times normalized to matched demand copying.
measured_compiler.pdf: five graph sizes, 100 trials each; median full compile,
one output-bound update, and single-distinct-input admission; both axes logarithmic.

The host was not dedicated and frequency was not fixed. Report the measured
variation and avoid drawing precise tail-latency or broad agent-speedup claims.
