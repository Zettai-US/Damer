#!/usr/bin/env python3
"""SSH control endpoint; scientific payloads only travel through CXLMemSim.

Each host fetches an immutable trajectory once per round (refetch), once per
loop (retain), or caches its unweighted sufficient statistics (statistics).
Two members on one host share that host cache. Numerical tasks fork and exit
before the host acknowledges completion. The trusted supervisor retains only
private copies; it has no mapped pointer into simulator memory.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
import argparse
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import sys
import time
import traceback
import numpy as np
from kernels import training_contribution, fit
from memsim_client import Client


def train(message, member, cache, conn, core):
    client = None
    try:
        os.sched_setaffinity(0, {core})
        start = time.perf_counter()
        cfg = message['config']
        n = cfg['n']
        gram = np.zeros((10, 10), np.float32)
        cross = np.zeros((10, 2*n*n), np.float32)
        weights = np.random.default_rng(cfg['seed']+1000*message['round']+member).uniform(
            .2, 1.8, len(message['dataset']))
        for desc, weight in zip(message['dataset'], weights):
            if cfg['policy'] == 'statistics':
                g, c = cache[desc['key']]
                gram += weight*g
                cross += weight*c
            else:
                g, c = training_contribution(cache[desc['key']], desc['params'], weight)
                gram += g
                cross += c
        model = fit(gram, cross, n)
        compute_s = time.perf_counter()-start
        client = Client(message['port'])
        start = time.perf_counter()
        digest = client.publish(message['models'][str(member)], model.tobytes(), 100+member)
        conn.send(dict(ok=True, member=member, pid=os.getpid(), core=core,
                       compute_s=compute_s, publish_s=time.perf_counter()-start,
                       model_sha256=digest, protocol=dict(client.stats)))
    except BaseException:
        conn.send(dict(ok=False, error=traceback.format_exc()))
    finally:
        if client is not None:
            client.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    client = Client(args.port)
    cache = {}
    cores = sorted(os.sched_getaffinity(0))
    inventory = dict(host=platform.node(), python=platform.python_version(), numpy=np.__version__,
                     kernel=platform.release(), cores=cores, machine_id=Path('/etc/machine-id').read_text().strip(),
                     source_hashes={p:hashlib.sha256(Path(__file__).with_name(p).read_bytes()).hexdigest()
                                    for p in ('kernels.py','memsim_client.py','memsim_worker.py')})
    print(json.dumps(dict(ok=True, inventory=inventory, pool=client.info)), flush=True)
    try:
        for line in sys.stdin:
            message = json.loads(line)
            start = time.perf_counter()
            children = []
            try:
                if message['op'] == 'reset':
                    cache.clear()
                    response = dict(ok=True, cache_bytes=0)
                elif message['op'] == 'ping':
                    response = dict(ok=True, server=client.call(0, 512, size=8)[0])
                elif message['op'] == 'counter':
                    before = dict(client.stats)
                    for _ in range(message['count']):
                        with client.lock(512, message['token']):
                            import struct
                            value = struct.unpack('<Q', client.call(0, 576)[1][:8])[0]
                            client.call(1, 576, data=struct.pack('<Q', value+1))
                    response = dict(ok=True, protocol={k:client.stats[k]-before[k] for k in before})
                elif message['op'] == 'train':
                    message['port'] = args.port
                    cfg = message['config']
                    if cfg['policy'] == 'refetch':
                        cache.clear()
                    before = dict(client.stats)
                    fetched = []
                    for desc in message['dataset']:
                        if desc['key'] in cache:
                            continue
                        raw = client.fetch(desc, 20)
                        data = np.frombuffer(raw, np.float32).reshape(desc['shape'])
                        data.flags.writeable = False
                        if cfg['policy'] == 'statistics':
                            cache[desc['key']] = training_contribution(data, desc['params'], 1.)
                        else:
                            cache[desc['key']] = data
                        fetched.append(dict(key=desc['key'], sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
                        del data, raw
                    fetch_s = time.perf_counter()-start
                    for i, member in enumerate(message['members']):
                        parent, child = mp.get_context('fork').Pipe()
                        proc = mp.get_context('fork').Process(target=train,
                            args=(message, member, cache, child, cores[i]))
                        proc.start()
                        child.close()
                        children.append((proc, parent))
                    members = []
                    for proc, pipe in children:
                        if not pipe.poll(180):
                            raise TimeoutError('training process')
                        row = pipe.recv()
                        proc.join(10)
                        if proc.is_alive() or proc.exitcode != 0 or not row['ok']:
                            raise RuntimeError(row)
                        members.append(row)
                    cache_bytes = sum(sum(x.nbytes for x in v) if isinstance(v, tuple) else v.nbytes
                                      for v in cache.values())
                    response = dict(ok=True, host=platform.node(), fetch_s=fetch_s,
                                    elapsed_s=time.perf_counter()-start, fetched=fetched,
                                    members=members, cache_bytes=cache_bytes,
                                    protocol={k:client.stats[k]-before[k] for k in before})
                else:
                    raise ValueError('unknown operation')
                print(json.dumps(response), flush=True)
            except BaseException:
                print(json.dumps(dict(ok=False, error=traceback.format_exc())), flush=True)
            finally:
                for proc, pipe in children:
                    if proc.is_alive():
                        proc.terminate()
                    proc.join(10)
                    pipe.close()
    finally:
        cache.clear()
        client.close()


if __name__ == '__main__':
    main()
