# arVM shared-state dataflow experiments

Real OpenCode tool calls access CXL shared objects through our DSec controller.
Trusted task effects describe sharing, publication dependencies, and future
uses. The runtime checks grants and pins; observed versions reconstruct
producer-consumer dataflow after execution.

Every data/control critical section uses the region-backed `SpinLock` from
[mutex_benchmark/fast_exp](https://github.com/EIRNf/mutex_benchmark/tree/fast_exp),
pinned to `55b707259c65d79b5f12a2f216b773c2a2a84124`.
Reads and writes are exclusive. Owner death causes timeout and fail-closed
behavior; the controller never forcibly unlocks potentially inconsistent state.

The manuscript is maintained separately in its Overleaf project. Damer contains
implementation, experimental evidence, and reproduction tools, not paper files.

## Evidence audit and successive fragments, 2026-10-11

The stronger controls narrow the result: **accepted future uses improve
retention over broad permissions; a DAG does not improve lifetime decisions
over a reference counter with the same information.** Independent reference
replay matches every graph-derived death time in all 12 original episodes,
with 288 allocation checkpoints checked. This is an information ablation,
not a performance comparison with a deployed object store.

Including registry, version records, and mutex pages changes the memory result:

| Average managed allocation, median of three trials | 2 reviewers | 4 reviewers |
|---|---:|---:|
| Scope-only | 911.7 KiB | 1343.6 KiB |
| Accepted object uses | 774.4 KiB | 1106.5 KiB |
| Reduction between those medians | 15.1% | 17.6% |
| Control allocation in the accepted-use mode | 327.9 KiB | 370.0 KiB |
| Payload allocation / logical content | 9.9x | 15.9x |

These numbers still exclude model, interpreter, RPC, page-table, and unrelated
memory. The older 22.4/23.3% result below counts only fixed-slot payloads.
An **offline** page-rounded peak-size replay yields 10.6/14.0% payload savings;
it uses retrospectively known output sizes and is not an implemented compact
allocator. A separate lossless-backing LRU replay reports transfers and backing
capacity without claiming measured I/O latency or unsafe recoverable eviction.

`DSec.replan` now atomically validates and replaces unstarted tasks, commits an
epoch, and updates pending-use counts. Existing authority limits the proposal;
new tasks require independent authorization. Running tasks, pins, and roots
survive. The caller commits new references before releasing bridging roots,
and admits tasks against the returned epoch. Replanning cannot resurrect
retired backing. Dynamic object creation, spilling, and pressure control are
not implemented.

Two additional **real OpenCode integration runs** use three successive
fragments each: draft, peer review, and synthesis. Both graph-scan and
future-reference-count modes complete all required steps: **10 task sessions,
58 operations, and complete payload reclamation**. The harness selects the
fragments. Two runs establish integration behavior, not comparative latency.

The current suite passes **20 CXL-backed unit/multiprocess tests**. A bounded
abstract model checks the lifetime invariant in 376 states and 1,210
transitions; deliberately weakened variants expose missing future references,
namespace drain, and resurrection guards. This is not an implementation proof.

The scaling study retains **all 60 trials**, including ten capacity failures.
At 65 tasks, median graph-mode pin/release cost is 2.108 ms. All ten 129-task
plans exceed the fixed 256 KiB registry. The first full replan costs 2.28 ms
at five tasks and 8.10 ms at 65, including cold control mapping. JSON processing,
audit logging, and CXL locks are included; RPC, payload access, inference, and
concurrent-controller throughput are not. The reference counter uses the same
metadata path and has similar costs. This implementation is not a scalable
constant-cost control path.

An 18-case tier control verifies that **shared DRAM and shared CXL both use
64 KiB payload PSS**. Private copies plus the retained CXL source use 192/320
KiB. Sharing semantics are not specific to CXL; this probe does not compare
tier latency. Every mode retains CXL locking.

- [Accounting and baseline audit](results/osdi-evidence-audit-20261011/summary.json), [figure](results/osdi-evidence-audit-20261011/accounting-audit.pdf), [bounded model](results/osdi-evidence-audit-20261011/lifetime-model.json)
- [Current tests and revision boundaries](results/osdi-evidence-audit-20261011/validation.json), [test output](results/osdi-evidence-audit-20261011/runtime-tests.txt)
- [Live registry scaling](results/osdi-registry-scale-20261011/summary.json), [all trials](results/osdi-registry-scale-20261011/trials.jsonl)
- [Successive-fragment suite](results/osdi-replan-live-20261011/suite.json), [graph run](results/osdi-replan-live-20261011/dataflow/manifest.json), [reference-count run](results/osdi-replan-live-20261011/refcount/manifest.json)
- [DRAM/CXL sharing control](results/osdi-tier-probe-20261011/runs.jsonl)

Archived runs retain their exact source revision. The current tests additionally
cover a later fix for legacy tasks whose grants define their effects: authority
updates now also update their future-use counters. The new performance and
fragment runs use explicit effects and do not exercise that compatibility path.

## Multi-round sharing and retention, 2026-10-11

The archived suite contains **12 episodes, 84 task sessions, and 484 object
operations** on actual CXL NUMA node 1. Two or four reviewer roles execute draft
and peer-review rounds, followed by synthesis. Each worker-count/policy pair
has three trials in seeded randomized blocks. All attempts publish outputs;
11/12 execute every required tool step. In `w4-scope-r1`, synthesis omits the
pre-write `answer` read, but its CAS publication succeeds. That trial remains
in every applicable aggregate.

The common input is an identical 43.6 KiB snapshot of four actual prototype
modules. The harness prescribes roles and accepted effects; the model performs
tool calls and generates review text. This is not a survey of naturally
discovered plans or an independent assessment of answer quality.
OpenCode v2.0.24 uses local Qwen3.5-35B-A3B-UD-Q4_K_XL on an RTX 5090.
Two 16K context slots serve both reviewer counts; four reviewers do not imply
four simultaneous inference streams. Each round starts a fresh task session
while preserving the logical reviewer identity.

| Metric | 2 reviewers | 4 reviewers |
|---|---:|---:|
| Scope-only average live payload, trial median | 575.7 KiB | 959.6 KiB |
| Dataflow average live payload, trial median | 446.5 KiB | 736.4 KiB |
| Reduction between those medians | 22.4% | 23.3% |
| Median byte-time reduction in same-timeline scope replay | 22.2% | 23.3% |
| Dataflow nonempty reads of versions shared across logical actors | 28/37, 75.7% | 51/66, 77.3% |
| Median dataflow corpus first-to-last-read span | 19.7 s | 37.4 s |
| Median union of corpus read critical sections | 24.2 ms | 43.4 ms |

Average live payload is the integral of allocated payload bytes divided by the
observation window. Fixed 64 KiB slots include unused output capacity. Registry,
version records, mutex pages, model memory, and RPC buffers are separate.
The table therefore does not report total process memory or CXL bus traffic.
The same-timeline replay is a capacity calculation, not another latency run.

Both policies use identical permissions, effects, slot sizes, and CXL locks.
Scope-only retains objects permitted to any unfinished task. Dataflow retains
objects required by accepted effects, active pins, or session roots. After task
namespaces exit, the controller actually closes and unlinks dead backing:
scratch after drafting, drafts after peer consumption, reviews after synthesis,
and the answer after its root is released. Uses outside accepted effects require
replanning. Unknown future turns require retained roots or recovery backing.

A concurrent PSS probe holds all worker mappings alive while checking
backing identity and actual page placement. Shared source mappings sum to
**64 KiB** for either worker count. Keeping the same CXL source plus private
DRAM replicas uses **192/320 KiB**, including 128/256 KiB of private DRAM.
Each condition has three measurements.

In an **offline idle-expiry replay**, 62/103 nonempty reads (60.2%) in the six
dataflow episodes occur after an inter-access gap exceeding one second. No
unsafe eviction is executed. These reads require retained or recoverable
backing; the result does not show that recoverable LRU eviction is unsafe.
Object versions, aliases, and future references inform last-use decisions.
Equivalent effect/reference tables can encode that information; a DAG is not
the uniquely necessary representation.

- [Summary and per-episode metrics](results/arvm-profile-20261011/summary.json)
- [Validation](results/arvm-profile-20261011/validation.json) and [all attempts](results/arvm-profile-20261011/episodes.jsonl)
- [Observed dataflow](results/arvm-profile-20261011/observed-dataflow.pdf), [retention and expiry replay](results/arvm-profile-20261011/sharing-profile.pdf), [lifetime timeline](results/arvm-profile-20261011/retention-timeline.pdf)
- [Concurrent PSS observations](results/arvm-sharing-probe-20261011/runs.jsonl)

## Initial live suite, 2026-10-10

This separate suite runs two prescribed workloads three times each, with two
analysts and a synthesizer. Runtime review uses the actual Registry source in
`fixtures/arvm_registry.py`; `fixtures/provenance.json` records its origin and
hash. Incident review uses an explicitly fictional incident record.

| Metric | Runtime review | Incident review |
|---|---:|---:|
| Attempted episodes / published outputs | 3 / 3 | 3 / 3 |
| Complete tool-step compliance | 2 | 3 |
| Object operations | 53 | 54 |
| Shared-version nonempty reads | 14/23, 60.9% | 17/24, 70.8% |
| Direct reads of another task's publication | 9 | 11 |
| Contended object-lock acquisitions | 2 | 2 |
| Median evidence access span | 8.532 s | 7.687 s |
| Median evidence critical-section union | 13.688 ms | 13.133 ms |
| Median episode duration | 17.054 s | 16.398 s |

All six attempts, 18 task sessions, and 107 operations are retained. There are
no tool errors or version conflicts. In `runtime-review-r0`, synthesis omits
the requested evidence reread but publishes its output. Six additional model
preflight sessions are excluded from the task-session count. Durations include
preflight, server startup, and cleanup. Answer quality has not been independently
scored. Plotted episode-ratio medians differ from the pooled fractions above.

- [Summary](results/arvm-live-final-20261010/summary.json), [attempts](results/arvm-live-final-20261010/episodes.jsonl), [validation](results/arvm-live-final-20261010/validation.json)
- [Figure](results/arvm-live-final-20261010/arvm-motivation.pdf), [model provenance](results/arvm-live-final-20261010/model-provenance.json)
- [Example contract](results/arvm-live-final-20261010/runtime-review-r2/arvm-memory-contract.json), [dataflow](results/arvm-live-final-20261010/runtime-review-r2/arvm-dataflow.json), [raw OpenCode calls](results/arvm-live-final-20261010/runtime-review-r2/analyst-0/opencode.jsonl)

## Grant performance and NVIDIA Sentry

Here, Grant means the local access lifecycle
`grant -> check/pin -> access -> release -> revoke`, not an identified external
system named Grant. The microbenchmark uses the same CXL registry and lock,
with 1/2/4 workers and five randomized trials each. It records 3,500
check/pin/release operations, 1,500 issuance/revoke pairs, and denial after
every revoke.

| Workers | Check + pin/release p50 | p95 | Issue p50 | Revoke p50 | Throughput |
|---|---:|---:|---:|---:|---:|
| 1 | 59.791 us | 71.639 us | 50.747 us | 29.735 us | 14,704 ops/s |
| 2 | 64.578 us | 77.688 us | 52.750 us | 31.177 us | 27,657 ops/s |
| 4 | 107.423 us | 173.003 us | 55.604 us | 33.952 us | 31,852 ops/s |

Cells are medians of five trial statistics, not confidence intervals. Issue
and revoke are measured by one controller after the concurrent phase. Warm
Python timings include JSON registry processing, CXL locking, and logging;
they exclude inference, payload access, and RPC. Throughput includes dispatch
and result collection. The serialized control section limits scalability.

[Raw trials](results/grant-final-20261010/runs.jsonl),
[summary](results/grant-final-20261010/summary.json), and
[validation](results/grant-final-20261010/validation.json) preserve this earlier
runtime revision; later implementation changes require fresh performance runs.

**NVIDIA Sentry has not been measured.** The host has no BlueField device or
configured Sentry endpoint/SDK. NVIDIA's
[architecture description](https://www.nvidia.com/en-us/solutions/ai/agent-safety/)
places its hardware isolation path on BlueField. OpenShell, gVisor Sentry, and
simulated delays are not substitutes for this baseline. Local MCP grants do not
provide equivalent DPU isolation or arbitrary-pointer revocation.

The [107-request comparison input](results/arvm-live-final-20261010/sentry-comparison/requests.jsonl)
and [comparison protocol](results/arvm-live-final-20261010/sentry-comparison/protocol.json)
are experimental formats, not an assumed NVIDIA API. A real comparison must
record hardware, Sentry/DOCA/firmware versions, actual policy/identity mapping,
transport, failures, and cold/warm/end-to-end timing boundaries.

## Implementation and interpretation

1. `dataflow.compile_memory_contract` validates trusted effects and dependencies,
   identifies common objects, publishers, writers, and pending consumers, and
   emits retention requirements. It does not infer access rights from natural
   language or analyze arbitrary native pointers.
2. Our DSec prototype manages scopes, admission, pins, generations, roots,
   retirement, and physical reclamation. It is separate from DeepSeek's system.
3. Payloads, version metadata, the registry, and mutex words use shared tmpfs
   mappings. `mbind` precedes first touch; `/proc/self/numa_maps` verifies actual
   residency, and sysfs decoder ranges establish node 1 as CXL. Disk JSON files
   are audit snapshots rather than live registry state.
4. An unmodified upstream SpinLock receives real CXL addresses through a small
   C ABI bridge compiled with `-Dcxl`. Unexpected emulated allocator calls abort.
5. Trusted MCP processes hold shared mappings. OpenCode and the model receive
   bounded copied text, so this is not zero-copy prompting or transparent
   protection against malicious native tools. Each task has a separate bwrap
   workspace/server; network isolation is not claimed.
6. RAW/RMW/WAR/WAW edges use observed object versions. `may_influence` edges
   conservatively connect earlier reads to later writes within a task; they do
   not prove semantic use of evidence by the model. Mutable versions reuse the
   same backing and are not persistent snapshots.

The ledger has multiple writers; append RMW edges preserve contributions.
Cross-task reads count explicit reads of another producer's version. Initial
live slots are 256 KiB; multi-round profile slots are 64 KiB. Checksums scan
padding. Instrumented scanned/copied bytes are not hardware bus transactions
and exclude copies in JSON, RPC, and model prompts. Locks are released between
tool calls. Revocation stops new grants and waits for pins; it does not revoke
arbitrary CPU pointers. Volatile CXL state is not crash-durable.

## Reproduction

Requires Linux, actual CXL NUMA memory, libnuma, g++, bwrap, and OpenCode v2.
Figures also require matplotlib and Graphviz. Source, library, and model hashes
accompany results. Native binaries, upstream checkouts, and runtime caches are
excluded from version control.

```bash
python3 workloads/agent_sharing/build_cxl_lock.py
python3 -m unittest discover -s workloads/agent_sharing -p 'test_*.py' -v

# Start the configured local model in a separate terminal.
python3 workloads/agent_sharing/serve_model.py

# Use new output directories for each experiment suite.
python3 workloads/agent_sharing/run_profile_suite.py \
  --output workloads/agent_sharing/results/my-profile --repeats 3
python3 workloads/agent_sharing/probe_sharing.py \
  --corpus workloads/agent_sharing/results/my-profile/w4-scope-r0/corpus.txt \
  --output workloads/agent_sharing/results/my-probe
python3 workloads/agent_sharing/report_profile.py \
  workloads/agent_sharing/results/my-profile \
  --probe workloads/agent_sharing/results/my-probe

# Rebuild archived figures without running the model or accessing live payloads.
python3 workloads/agent_sharing/report_profile.py \
  workloads/agent_sharing/results/arvm-profile-20261011 \
  --probe workloads/agent_sharing/results/arvm-sharing-probe-20261011

# Earlier workload and Grant suites.
python3 workloads/agent_sharing/run_live_suite.py \
  --output workloads/agent_sharing/results/my-live --repeats 3
python3 workloads/agent_sharing/bench_grant.py \
  --output workloads/agent_sharing/results/my-grant --repeats 5 --steps 100
python3 workloads/agent_sharing/summarize_live.py \
  workloads/agent_sharing/results/my-live --grant workloads/agent_sharing/results/my-grant
python3 workloads/agent_sharing/export_sentry_workload.py \
  workloads/agent_sharing/results/my-live
python3 tools/arvm_shared_state.py \
  workloads/agent_sharing/results/my-live/runtime-review-r0
```

The original shared-state suite had 12 unit/multiprocess checks, including
concurrent appends, CAS conflicts, corruption, owner death, grants, dependencies,
roots, future uses, version lineage, and actual reclamation. A separate CXL
control smoke test covers 26 conditions and 150 operations; it is not an LLM
motivation result or a Sentry performance comparison.

Additional reproduction commands (use fresh output paths):

```bash
# Offline audits: no model service or surviving CXL payloads are needed.
python3 workloads/agent_sharing/audit_profile.py \
  workloads/agent_sharing/results/arvm-profile-20261011 \
  --output workloads/agent_sharing/results/my-accounting-audit
python3 workloads/agent_sharing/check_lifetime_model.py \
  --output workloads/agent_sharing/results/my-lifetime-model.json

# Requires actual CXL; the scaling run does not need a model.
python3 workloads/agent_sharing/bench_registry_scale.py \
  --output workloads/agent_sharing/results/my-registry-scale
python3 workloads/agent_sharing/probe_sharing.py \
  --corpus workloads/agent_sharing/results/arvm-profile-20261011/w4-scope-r0/corpus.txt \
  --include-shared-dram --output workloads/agent_sharing/results/my-tier-probe

# Requires the configured local model service.
python3 workloads/agent_sharing/run_replan_live.py --policy dataflow \
  --output workloads/agent_sharing/results/my-replan-dataflow
python3 workloads/agent_sharing/run_replan_live.py --policy refcount \
  --output workloads/agent_sharing/results/my-replan-refcount
```

## Archive scope

Formal evidence comes from `arvm-live-final-20261010`,
`arvm-profile-20261011`, `arvm-sharing-probe-20261011`, and
`grant-final-20261010`, plus the four dated `osdi-*` suites linked above.
The directory names identify the research audit, not a claim of venue readiness.
`profile-development` and older exploratory directories
are excluded. Historical file-lock runs and setup troubleshooting are not part
of current CXL claims. All `.dsec-state` runtime caches remain untracked.
