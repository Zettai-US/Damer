# Scientific discovery loops on CXL

This benchmark executes **simulate → retain fields → train surrogate ensemble
→ select two candidates → simulate again** in separate CPU worker processes.
It reuses the independently implemented DSec controller in `agent_sharing` and
the pinned upstream CXL SpinLock. No flock, artificial latency, or global cache
dropping is used in the physical-CXL path. `run.py` measures a single physical
CXL host. `run_memsim.py` executes the same discovery algorithm across three
machines using CXLMemSim's TCP memory server and server CAS locks. The two
result populations are separate; neither evaluates GPUs.

The workloads are periodic Gray–Scott reaction–diffusion and damped-wave PDEs.
Each produces 16 snapshots of two float32 fields. A weighted polynomial ridge
ensemble predicts the entire field; candidate selection ranks disagreement at
a fixed spatial sample. Four iterations retain 4, 6, 8, and 10 trajectories.
The two parameters selected after each iteration actually drive the following
simulations. These are controlled numerical workloads, not validated scientific
discovery campaigns or competitive neural operators.

## Data paths

| Mode | Publication and consumption | Important boundary |
|---|---|---|
| `file-mmap` | Buffered ext4 file; consumers mmap it | Linux page cache can eliminate physical disk reads |
| `file-durable` | Flush/fsync publication; reusable DRAM staging | Stronger durability than volatile memory modes |
| `dram-shared` | Shared tmpfs pages bound to node 0 | Same sharing semantics, different tier |
| `cxl-shared` | Shared tmpfs pages bound to verified CXL node 1 | No per-member input replicas |
| `cxl-copy` | CXL source plus reusable DRAM staging | Allocation is reused; bytes are recopied |
| `cxl-cache` | CXL source plus a persistent DRAM replica per member | Copies each trajectory once and retains it across iterations |

All modes use the same CXL control registry, task effects, and numerical
algorithm. Private solver arrays, Gram matrices, and field cross-products
are private CPU allocations under the host's default NUMA policy; their
physical tier is not independently verified. A model is immutable by trusted protocol, not
OS-sealed against hostile native code. A task is completed only after its
process exits, so its mappings have drained. Model versions retire after
selection; corpus roots remain until the loop ends.

The bulk ledger implements a fixed fail-closed per-loop allocation budget. It
does not implement spill, migration, dynamic fabric allocation, or multi-tenant
capacity pooling. DRAM cache allocations are counted separately and are not
charged to that bulk budget. Allocation failure aborts the trial; no fallback
quietly changes the memory tier.

## Reproduce

Requires Linux, physical CXL exposed as NUMA node 1, DRAM node 0, libnuma,
Python and NumPy. First build the lock bridge using
`python3 workloads/agent_sharing/build_cxl_lock.py` (see that directory's
README for the compiler/upstream requirements).

```bash
python3 workloads/science_loop/test_science.py
python3 workloads/science_loop/run.py --suite smoke --repeats 1 --output /path/to/new-smoke
python3 workloads/science_loop/run.py --suite main --repeats 5 --output /path/to/new-study/main
python3 workloads/science_loop/run.py --suite stress --repeats 5 --output /path/to/new-study/stress
python3 workloads/science_loop/run.py --suite compute --repeats 5 --output /path/to/new-study/compute
python3 workloads/science_loop/run.py --suite refs --repeats 5 --output /path/to/new-study/refs
python3 workloads/science_loop/run.py --suite scale --repeats 5 --output /path/to/new-study/scale
python3 workloads/science_loop/run.py --suite instrumentation --repeats 5 --output /path/to/new-study/instrumentation
python3 workloads/science_loop/analyze.py /path/to/new-study
```

Run performance suites sequentially. The stress suite intentionally launches
an owned 128 MiB CXL streaming competitor, records its work, and terminates it
after each loop. Trial directories must not already exist. Raw field files are
deleted after use; task traces, numerical hashes, predictions, page residency,
allocation accounting, I/O counters, faults, and source snapshots are retained.

## Measurement contract

- The primary observation is a four-iteration loop. A plotted time per
  iteration is its total divided by four, not four independent replicates.
- Timing begins before each fragment commit and includes process creation,
  simulation, synchronization, publication, mapping/copying, fitting, selection,
  profiling, and model retirement. Setup and final corpus cleanup are separate.
- Five complete randomized blocks use matched seeds 712–716. No measured
  warmup is discarded. Min/max whiskers are block ranges, not confidence
  intervals. The analysis also emits descriptive paired-block bootstraps.
- Numerical equivalence requires identical selected IDs, full trajectory
  hashes, and held-out prediction hashes for matched kernel/grid/steps/member
  count/seed. The test set never drives selection.
- Full profiling touches and inspects every mapping, including ordinary file
  mappings. Bound tiers require exact physical page counts. A bounded refault
  retry is recorded if reclaim races a snapshot; wrong/incomplete residency is
  never accepted by relaxing the count.
- The instrumentation suite compares full checks with publication-only
  residency checks. Hashes, DSec checks and prediction validation remain on.
  Unchecked read aliases in the reduced mode are explicitly tagged.
- Managed backing includes actual source and replica file capacities plus
  controller allocations. It is not total application RSS. Ordinary file
  capacity must not be called resident RAM; memory comparisons exclude it.
- Logical copies, worker I/O counters, and CXL bus traffic are different
  metrics. The benchmark does not collect hardware bus counters.
- The host is shared and has pre-existing swap use. Global memory and load
  snapshots are archived; available node-0 capacity and background workloads
  are not isolated. Results are conditional on this host, not an intrinsic
  DRAM/CXL bandwidth comparison or a controlled capacity-pressure curve.
- Four-member competitor on/off configurations share randomized stress
  blocks. Two-member no-competitor data come from the main suite, so their
  cross-suite contrast includes temporal variation; the paper uses the
  four-member within-suite contrast for its primary contention statement.

The experiment can establish conditional capacity/latency tradeoffs. It cannot
establish HBM access, cross-host coherence, greater-than-DRAM capacity benefits,
scientific usefulness of the small surrogate, or superiority to Ray, Colmena,
Parsl, SmartSim, or NVIDIA Sentry. The installed system PyTorch could not import;
the reported implementation is CPU-only.

## Three-machine CXLMemSim path

The coordinator simulates fields, commits DSec fragments, and selects the next
experiments. Two remote machines train one model member each. Controls place
both members on either remote machine, sharing a host-local input cache. All
bulk data and live DSec registry bytes use the **actual CXLMemSim TCP protocol**;
SSH stdin/stdout carries commands, descriptors, hashes and metrics only. Private
loopback reverse tunnels carry TCP memory operations. No scientific array is
sent through the control messages. This is an explicitly instrumented memory
client, not QEMU guest load/store execution or a physical coherent CXL fabric.

The server runs a fresh 64 MiB pool for every loop, with topology `(1);`,
`default_latency=100`, and DCD/GFAM disabled. This configuration has one memory
server and multiple client hosts. The server's cache identities are connections,
not physical CPU caches. Build and source hashes, server arguments and each
host's software versions are archived. The backing file is unique and lives
in local tmpfs; server backing pages are not asserted to be physical CXL.

The adapter uses server `OP_ATOMIC_CAS` for both registry and publication locks.
The existing DSec algorithm validates effects, checks epochs, tracks future
uses, and holds pins across remote dispatch and completion. It rejects
premature retirement and undeclared accesses. The two model slots are recycled
after selection drains, with version checks rejecting stale descriptors. The
training corpus remains rooted until loop completion. This cooperative client
protocol does not prevent a malicious TCP client from issuing arbitrary reads.

| Policy | Remote consumption | Cache accounting |
|---|---|---|
| `refetch` | Each host fetches the whole accumulated corpus every round | Its model members share the current host cache |
| `retain` | Each host fetches each trajectory once across four rounds | Keeps one private trajectory replica per host |
| `statistics` | Fetch once and retain unweighted Gram/cross-product contributions | Reuses sufficient statistics; does not retain the raw local trajectory |

The statistics baseline implements an application-specific optimization of
this polynomial learner. It applies each round's new member weights to cached
unweighted contributions. It demonstrates why avoiding redundant computation
or changing data representation can matter more than sharing raw fields.
Its result must match the other policies for the same host placement.

**Timing interpretation:** measured seconds include the Python adapter,
CXLMemSim server execution, TCP, SSH encryption, process lifecycle, checks,
metadata and numerical work. The server returns modeled nanoseconds without
delaying each operation by that amount. Their sum is retained as a diagnostic,
never used as an E2E prediction or added to measured wall time. No latency or
bandwidth sensitivity claim follows from merely setting these parameters.
The source's congestion calculation also has its own transfer-cost formula;
the nominal endpoint bandwidth is not a calibrated sustained-fabric result.
Host timestamps are not assumed synchronized; only coordinator intervals are
used for E2E time. Per-host durations are not summed across overlapping work.

Private cache bytes, live logical payload bytes and reserved pool-address bytes
are distinct metrics. None includes Python heaps, fork page tables, server
coherence metadata, wire framing, or total machine RSS. Local compute arrays
are private copies; this TCP path does not establish zero-copy remote access.

Build the pinned CXLMemSim revision recorded in the manifest (C++20 compiler,
CMake 3.25+, spdlog required), then run:

```bash
/usr/bin/cmake -S /path/to/CXLMemSim -B workloads/science_loop/build/memsim \
  -DCMAKE_BUILD_TYPE=Release -DCXLMEMSIM_BUILD_MICROBENCHMARKS=OFF \
  -DCXLMEMSIM_ENABLE_SLUGALLOCATOR=OFF -DCXLMEMSIM_ENABLE_RDMA=OFF
/usr/bin/cmake --build workloads/science_loop/build/memsim --target cxlmemsim_server -j4
python3 workloads/science_loop/test_memsim.py -v
python3 workloads/science_loop/run_memsim.py --hosts HOST_A HOST_B \
  --memsim-source /path/to/CXLMemSim --suite smoke --repetitions 1 --output /path/to/smoke
python3 workloads/science_loop/run_memsim.py --hosts HOST_A HOST_B \
  --memsim-source /path/to/CXLMemSim --suite main --repetitions 5 --output /path/to/study/main
python3 workloads/science_loop/run_memsim.py --hosts HOST_A HOST_B \
  --memsim-source /path/to/CXLMemSim --suite placement --repetitions 5 --output /path/to/study/placement
python3 workloads/science_loop/analyze_memsim.py /path/to/study
```

Hosts need existing trusted SSH keys, reverse forwarding, Linux and Python
3.8+ with NumPy. Only three worker source files are deployed into a unique
temporary directory. The runner removes its own workers, tunnels, simulator
backing and remote deployment. It leaves all pre-existing processes alone.
The main suite crosses two solvers, grids 32/64, three policies and five seed
blocks (60 loops); placement adds both members on each remote host at grid 64
with retained inputs (20 loops). Four correlated iterations form one timing
observation. The small grids bound the cost of the simulator protocol with at most 64 payload bytes per request.

The audit requires exact hashes and candidate identities across policies at
the same placement. Across heterogeneous NumPy/LAPACK hosts it additionally
checks candidate identities and saved prediction/score arrays with declared
`rtol=1e-3, atol=1e-4`; observed errors are reported. Every fetched trajectory
must match its producer's hash, and expected fetch counts, model hashes,
retirements, source hashes and final empty pins/roots are independently checked.

The manuscript and publication figures remain in the separate manuscript
repository and are not part of the Damer code/data push.

## With and without Damer

`run_damer_ablation.py` compares the integrated Damer DSec controller against
`barrier_control.py`, a hand-written, fixed-stage baseline for this exact
scientific loop. Both arms use the same numerical code, seed, candidate policy,
worker placement, cache policy, immutable payload publication, CXL locks and
process-exit handoff boundary. No baseline is forced to copy extra data.

| Mechanism | With Damer | Without Damer: fixed-stage baseline |
| --- | --- | --- |
| Scientific algorithm and data policy | Identical | Identical |
| Payload publication and budget | CXL lock / CAS | Same CXL lock / CAS |
| Task ordering | Declared dependencies and harness stages | Harness stages and manual barriers |
| Object authority/effect validation | DSec | Not provided |
| Plan epoch validation | DSec | Not provided |
| Per-object access pins and future uses | DSec registry | Not provided |
| Corpus lifetime and model reuse | Roots and checked retirement | Manually encoded lifetime and stage completion |
| Allocation accounting | Shared ledger in physical study; preallocated arena in simulator | Same ledger or arena |
| Scheduling metadata | Shared JSON registry | Coordinator-private stage records |

This is an ablation of the **DSec integration**, not of a complete Damer
compiler, hardware enforcement, or a production distributed runtime. The
baseline is safe only for the fixed, trusted workflow whose stages and joins
are encoded in the harness. It has no equivalent general access authorization
or in-flight replan contract. Additional metadata traffic in the Damer arm
measures the current registry representation as well as its checks; it is not
an intrinsic lower bound on dataflow enforcement.

The physical study uses grids of 256 squared, two solvers, three data policies
(CXL sharing, retained DRAM replicas, and file mmap), and five randomized seed
blocks: 60 loops. The three-host CXLMemSim study uses grids of 64 squared and
refetch/retain/statistics policies with the same factorial structure: another
60 loops. Each loop contains four dependent scientific iterations. Controller
arms are paired by seed within randomized blocks; repetitions, not iterations,
are the independent timing observations. Timing intervals exclude setup and
final cleanup, which are reported separately. Do not compare physical and
simulator seconds as a CXL tier penalty.

```bash
python3 workloads/science_loop/run_damer_ablation.py --domain physical \
  --output /new/output/physical --repetitions 5
python3 workloads/science_loop/run_damer_ablation.py --domain memsim \
  --output /new/output/memsim --repetitions 5
python3 workloads/science_loop/analyze_damer_ablation.py /new/output
```

The analyzer requires a complete pair for every policy/seed, identical
trajectory and prediction hashes and selected candidates, matching payload
capacities and copy/fetch counts, correct tier residency, and fully drained
lifetimes. `contrasts.json` reports with/without runtime ratios (above one means
Damer costs time), absolute differences and descriptive paired bootstrap
intervals. Controller arms are never pooled as repeated samples of one method.

### Splash integration boundary

Splash's explicit read/write, bulk, CAS and fence interfaces are a suitable
attachment point for object/version/range access records. They do not alone
identify a logical task's future uses, authority or completion; those remain
Damer's responsibility. The inspected local Splash revision is
`6996e71c0de57c861935222e438786884044555c`. Its SHMEM backend uses POSIX shared
memory for local request slots and copies payloads. The backend factory directs
TCP callers to the separate CXLMemSim client rather than constructing a TCP
backend. Consequently, SHMEM alone does not establish cross-host zero-copy
sharing. The current three-host experiment uses the direct CXLMemSim protocol
adapter and **does not yet benchmark the Splash library**. A later Splash
comparison must hold its backend, cache policy, locking and access granularity
constant across both controller arms.

The client capacity guard was tightened after the initial 80-loop simulator
study: the server's reported backing size includes its header, while the
addressable data capacity is the number of data cachelines times 64. All
initial-study addresses were below 7 MiB in a 64 MiB pool with an additional
4 KiB guard, so none used the excluded last line. The paired ablation uses the
corrected boundary guard; archived snapshots retain the exact original code.

The simulator reserves a fixed address arena during setup in both arms. Its
64 MiB server backing is not dynamically resized; logical retirement permits
versioned model-slot reuse and removes lifetime obligations. The physical
experiment instead updates a shared live-byte ledger and unlinks retired bulk
backing. These are different allocation mechanisms and are not pooled in one
capacity statistic. `profile_controller.py` partitions simulator coordinator
wall time into protocol I/O, remote-stage waiting, and the local remainder;
protocol I/O contains both control and payload accesses.

## Audited results: October 20, 2026

The final artifact contains 380 physical-CXL loops, 80 three-host simulator
loops, and 120 separately paired controller-ablation loops: 580 complete loops
and 2,320 scientific iterations. All three population audits pass. Earlier
attempts and smoke runs remain in a separate method-history archive.

The table reports the median of five seed-paired with/without-Damer time
ratios. Values above one indicate additional time with DSec. Intervals are
descriptive 95% paired bootstraps with only five blocks on shared hosts;
small differences are not stable production guarantees.

| Domain | Solver | Data policy | With / without | Bootstrap interval |
| --- | --- | --- | ---: | --- |
| memsim | gray-scott | refetch | 2.81 | [2.80, 2.82] |
| memsim | gray-scott | retain | 4.12 | [4.10, 4.14] |
| memsim | gray-scott | statistics | 4.16 | [4.07, 4.20] |
| memsim | wave | refetch | 2.84 | [2.81, 2.86] |
| memsim | wave | retain | 4.15 | [4.12, 4.18] |
| memsim | wave | statistics | 4.12 | [4.09, 4.22] |
| physical | gray-scott | cxl-cache | 1.04 | [0.82, 1.17] |
| physical | gray-scott | cxl-shared | 1.15 | [1.01, 1.27] |
| physical | gray-scott | file-mmap | 0.99 | [0.92, 1.15] |
| physical | wave | cxl-cache | 1.04 | [1.00, 1.17] |
| physical | wave | cxl-shared | 1.10 | [0.92, 1.30] |
| physical | wave | file-mmap | 1.17 | [0.82, 1.53] |

The simulator contrast adds 4.56–4.57 seconds of coordinator protocol I/O
per iteration (median paired differences across conditions). This reflects
the current full-registry serialization/transport path, not a hardware-CXL
latency estimate or an unavoidable cost of dataflow analysis. The full
profile partitions protocol I/O, remote-stage waiting and local remainder
on the coordinator clock. Both controller arms preserve trajectory and
prediction hashes, candidate choices, and identical payload/copy accounting.

See [the evidence index](artifacts/README.md) for complete inventories, source
snapshots, CSV observations, paired contrasts and validation reports. The
manuscript and publication figures are deliberately excluded.
