# Scientific-loop evidence

The three final populations are distinct:

- `science-e2e-final-20261020.tar.gz`: physical-CXL single-host CPU loops.
- `memsim-e2e-20261020.tar.gz`: multi-host functional-simulator loops. Measured
  seconds include CXLMemSim and SSH/TCP, not predicted hardware-CXL runtime.
- `damer-ablation-20261020.tar.gz`: paired with/without DSec integration,
  separately grouped by physical host and three-host simulator domain.
- `method-history-20261020.tar.gz`: earlier attempts, including a strict page
  residency failure, a superseded instrumentation revision, and protocol smoke
  runs. These are retained for audit, not combined with final measurements.

Each final archive contains source snapshots, manifests, complete trial
inventories, controller traces, numerical checks and analysis outputs. The
adjacent directories expose CSV summaries and JSON audits for review.
`SHA256SUMS.json` gives archive hashes and audited population sizes.

After extracting into a new directory, rerun `analyze.py` for the physical
population and `analyze_memsim.py` for the simulator population. The latter
checks saved NumPy predictions as well as transfer counts and lifetime state.
Use `analyze_damer_ablation.py` for the paired controller population; do not
merge its controller arms into the previous population.
Run from a checkout containing the scientific harness and its agent_sharing
controller dependency. Full numerical arrays are recreated from archived
seeds and kernel source; they are not required to recheck the stored results.

The manuscript and publication figure pack are maintained separately and are
intentionally absent from this code/data repository.
