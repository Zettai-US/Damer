#!/usr/bin/env python3
"""Launch the locally configured Qwen model used by the live experiment."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import os

SERVER=Path('/root/qwen35-a3b-build/bin/llama-server')
MODEL=Path('/root/models/qwen35-a3b/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf')


def command(server=SERVER,model=MODEL):
    return ['numactl','--membind=1',str(server),'--model',str(model),'--alias','damer-qwen35',
        '--host','127.0.0.1','--port','18081','--gpu-layers','99','--ctx-size','32768',
        '--parallel','2','--threads','8','--jinja','--reasoning','off',
        '--chat-template-kwargs','{"enable_thinking":false}','--no-webui']


def provenance():
    # Hash the actual model and executable; no API credentials in artifacts.
    def digest(path):
        with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
    return dict(model=str(MODEL),model_bytes=MODEL.stat().st_size,model_sha256=digest(MODEL),
        server=str(SERVER),server_sha256=digest(SERVER),argv=command(),
        server_version=subprocess.check_output([str(SERVER),'--version'],stderr=subprocess.STDOUT,text=True).strip(),
        gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv,noheader'],text=True).strip(),
        placement='model inference offloaded to GPU; host model mapping bound to CXL node 1; shared-state placement verified separately')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provenance',type=Path)
    a=p.parse_args()
    if a.provenance:a.provenance.write_text(json.dumps(provenance(),indent=2)+'\n')
    else:os.execvp('numactl',command())
