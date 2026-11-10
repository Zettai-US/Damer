#!/usr/bin/env python3
"""Archive randomized paired local controls without conflating them with NCCL."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import random
import subprocess
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--trials', type=int, default=5)
    ap.add_argument('--n', type=int, default=64)
    ap.add_argument('--steps', type=int, default=64)
    ap.add_argument('--repeats', type=int, default=20)
    args = ap.parse_args()
    if args.trials < 1:
        ap.error('trials must be positive')
    binary = args.binary.resolve()
    # Refuse to overwrite an evidence directory from another run.
    args.output.mkdir(parents=True, exist_ok=False)
    sources = Path(__file__).resolve().parent
    manifest = dict(schema=1, time_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    machine=platform.machine(), platform=platform.platform(),
                    binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(sources.iterdir()) if p.suffix in {'.cu', '.cuh', '.py', '.txt'}},
                    options={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                    scope='Single GPU fixed DAG; no NCCL/GIN, CXL, SSD, or full discovery-loop performance claim.')
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    rng = random.Random(20261110)
    with (args.output / 'trials.jsonl').open('w') as out:
        for trial in range(args.trials):
            modes = ['persistent', 'host', 'graph']
            rng.shuffle(modes)
            for mode in modes:
                command = [str(binary), '--mode', mode, '--n', str(args.n), '--steps', str(args.steps),
                           '--repeats', str(args.repeats)]
                p = subprocess.run(command, capture_output=True, text=True, timeout=120)
                if p.returncode:
                    raise RuntimeError(f'{command}: {p.stdout}\n{p.stderr}')
                row = json.loads(p.stdout)
                if not row['valid']:
                    raise RuntimeError('validation failed')
                row.update(trial=trial, command=command)
                out.write(json.dumps(row)+'\n')
                out.flush()
                print(f"trial={trial} mode={mode} gpu_ms={row['gpu_ms']:.3f} cpu_ms={row['process_cpu_ms']:.3f}", flush=True)


if __name__ == '__main__':
    main()
