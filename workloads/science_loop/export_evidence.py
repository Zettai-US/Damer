#!/usr/bin/env python3
"""Export numerical code/data evidence only. Manuscript paths are not inputs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tarfile


def archive(target, sources):
    with target.open('wb') as raw, gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as gz:
        with tarfile.open(fileobj=gz,mode='w',format=tarfile.PAX_FORMAT) as tf:
            for source in sources:
                for path in [source]+sorted(source.rglob('*')):
                    if path.is_symlink():
                        raise ValueError('refusing external symlink in evidence')
                    name=Path(source.name)/path.relative_to(source)
                    info=tf.gettarinfo(str(path),arcname=str(name))
                    info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
                    if path.is_file():
                        with path.open('rb') as f:tf.addfile(info,f)
                    else:tf.addfile(info)


def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    sources=[args.physical,args.simulator,args.ablation]
    audits={}
    for source in sources:
        audit=json.loads((source/'analysis/validation.json').read_text())
        if audit['status']!='pass' or audit.get('failures',[]):
            raise ValueError('evidence must pass its audit before export')
        audits[source.name]=dict(loops=audit['loops'],iterations=audit['iterations'],status=audit['status'])
        archive(args.output/(source.name+'.tar.gz'),[source])
        dest=args.output/source.name;dest.mkdir(exist_ok=True)
        for p in (source/'analysis').iterdir():
            if p.is_file():
                if p.suffix=='.csv':(dest/p.name).write_text(p.read_text())
                else:shutil.copy2(p,dest/p.name)
    if args.history:
        archive(args.output/'method-history-20261020.tar.gz',args.history)
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in args.output.glob('*.tar.gz')}
    manifest=dict(archives=hashes,final_populations=audits,
                  history='Earlier attempts and superseded revisions; never pooled into final timing statistics.',
                  excluded='No manuscript, paper figures, SSH configuration, model credentials, or external files.')
    (args.output/'SHA256SUMS.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (args.output/'README.md').write_text('''# Scientific-loop evidence

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
''')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--physical',type=Path,required=True)
    parser.add_argument('--simulator',type=Path,required=True)
    parser.add_argument('--ablation',type=Path,required=True)
    parser.add_argument('--history',type=Path,nargs='*',default=[])
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
