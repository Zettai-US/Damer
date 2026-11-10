#!/usr/bin/env python3
"""Read-only machine inventory; no package installs, sysctls or network changes."""
import json
import platform
import shutil
import subprocess
from pathlib import Path


def command(args):
    if not shutil.which(args[0]):
        return dict(available=False)
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return dict(available=True, returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
    except subprocess.TimeoutExpired:
        return dict(available=True, timeout=True)


def main():
    print(json.dumps(dict(
        machine=platform.machine(), platform=platform.platform(),
        gpu=command(['nvidia-smi', '--query-gpu=name,compute_cap,driver_version', '--format=csv,noheader']),
        nvcc=command(['nvcc', '--version']), cmake=command(['cmake', '--version']),
        rdma=command(['ibv_devices']), rdma_links=command(['rdma', 'link', 'show']),
        nccl_packages=command(['dpkg-query', '-W', 'libnccl2', 'libnccl-dev']),
        memory=Path('/proc/meminfo').read_text(),
        note='Inventory does not establish GIN, GPUDirect RDMA, or remote CXL support.'
    ), indent=2))


if __name__ == '__main__':
    main()
