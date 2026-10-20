#!/usr/bin/env python3
"""Actual multiprocess scientific discovery loops on verified CXL memory.

No simulated latency, flock, cache dropping, cross-host or GPU claims. A small
descriptor adapter reuses the existing DSec controller and upstream CXL lock.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'
import argparse
import ctypes
from contextlib import contextmanager
import hashlib
import json
import mmap
import multiprocessing as mp
from pathlib import Path
import random
import resource
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'agent_sharing'))
from store import Store, bind, address, placement, verify_placement, hardware, dump
from dsec import DSec
from kernels import simulate, basis, candidates, training_contribution, fit

MODES = ['file-mmap', 'file-durable', 'dram-shared', 'cxl-shared', 'cxl-copy', 'cxl-cache']
NOW = time.perf_counter
# Resolve libnuma once, rather than invoking the library-discovery helper for
# every bulk mapping. The existing controller remains unchanged in all paths.
NUMA=ctypes.CDLL('libnuma.so.1',use_errno=True)
NUMA.mbind.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,
                    ctypes.POINTER(ctypes.c_ulong),ctypes.c_ulong,ctypes.c_uint]
NUMA.mbind.restype=ctypes.c_int


def bulk_bind(buf,node):
    mask=ctypes.c_ulong(1<<node)
    if NUMA.mbind(address(buf),len(buf),2,ctypes.byref(mask),8*ctypes.sizeof(mask),0):
        raise OSError(ctypes.get_errno(),'bulk mbind failed; no fallback')


def io_counts():
    return {k: int(v) for k, v in (x.split(':') for x in Path('/proc/self/io').read_text().splitlines())}


class Buffer:
    def __init__(self, path, shape, create=False, node=None):
        self.path = Path(path)
        self.shape = tuple(shape)
        self.size = int(np.prod(shape))*4
        if self.size % mmap.PAGESIZE:
            raise ValueError('bulk objects must be page aligned')
        fd = os.open(path, os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
        try:
            if create:
                os.ftruncate(fd, self.size)
            elif os.fstat(fd).st_size != self.size:
                raise ValueError('bulk shape does not match backing')
            self.mm = mmap.mmap(fd, self.size)
        finally:
            os.close(fd)
        if node is not None:
            bulk_bind(self.mm, node)
        self.array = np.ndarray(shape, dtype=np.float32, buffer=self.mm)
        self.node = node

    def validate(self):
        # NUMA accounting is a snapshot: reclaim can remove a page between
        # touching the mapping and reading numa_maps. Require an exact match;
        # bounded refault/rechecks are recorded, never a relaxed page count.
        self.check_retries = []
        for attempt in range(5):
            _ = sum(self.mm[::mmap.PAGESIZE])
            try:
                if self.node is not None:
                    return verify_placement(self.mm, self.node)
                # Inspect ordinary file mappings too. Their tier is observed,
                # not forced; all comparisons pay the same snapshot machinery.
                pages=placement(self.mm)
                if sum(pages.values())!=self.size//mmap.PAGESIZE:
                    raise RuntimeError(f'file residency incomplete: {pages}')
                return pages
            except RuntimeError as error:
                self.check_retries.append(str(error))
                if attempt == 4: raise

    def close(self):
        del self.array
        self.mm.close()


class Bulk:
    def __init__(self, cfg, store, task):
        self.cfg, self.store, self.task = cfg, store, task
        self.dsec = DSec(store)
        self.timings = dict(load_s=0., publish_s=0., checks_s=0.)
        self.bytes = dict(local_copy=0, file_read=0, file_write=0)
        self.placements = []

    def path(self, key):
        return Path(self.cfg['bulk'])/key

    @contextmanager
    def output(self, key, shape):
        size = int(np.prod(shape))*4
        with self.dsec.pin(self.task, key, 'write'):
            with self.dsec.registry() as state:
                pool = state['bulk']
                if key in pool['objects']:
                    raise ValueError('immutable version already allocated')
                if pool['live_bytes'] + size > pool['budget_bytes']:
                    raise MemoryError('managed bulk budget exhausted')
                pool['objects'][key] = size
                pool['live_bytes'] += size
                pool['peak_bytes'] = max(pool['peak_bytes'], pool['live_bytes'])
            node = None if self.cfg['mode'].startswith('file-') else (0 if self.cfg['mode']=='dram-shared' else 1)
            start = NOW()
            buf = Buffer(self.path(key), shape, create=True, node=node)
            self.timings['publish_s'] += NOW()-start
            try:
                yield buf.array
                start = NOW()
                if self.cfg['mode']=='file-durable':
                    buf.mm.flush()
                    fd = os.open(self.path(key), os.O_RDWR)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                if self.cfg['mode'].startswith('file-'):
                    self.bytes['file_write'] += size
                self.timings['publish_s'] += NOW()-start
                start = NOW()
                self.placements.append(dict(key=key, node=node, pages=buf.validate(),
                                            residency_rechecks=buf.check_retries))
                self.timings['checks_s'] += NOW()-start
            finally:
                buf.close()

    @contextmanager
    def read(self, key, shape):
        with self.dsec.pin(self.task, key, 'read'):
            start = NOW()
            mode = self.cfg['mode']
            node = None if mode.startswith('file-') else (0 if mode=='dram-shared' else 1)
            src = Buffer(self.path(key), shape, node=node)
            private = None
            try:
                if mode in {'cxl-copy', 'cxl-cache', 'file-durable'}:
                    # A persistent per-logical-worker replica is the stronger
                    # copy control. cxl-copy reuses its staging allocation.
                    cache_id = self.task.split('-r')[0] if mode=='cxl-cache' else self.task.split('-r')[0]+'-staging'
                    name = key if mode=='cxl-cache' else str(src.size)
                    cache = Path(self.cfg['cache'])/(cache_id+'-'+name)
                    exists = cache.exists()
                    private = Buffer(cache, shape, create=not exists, node=0)
                    if not exists or mode!='cxl-cache':
                        private.array[:] = src.array
                        self.bytes['local_copy'] += src.size
                        if mode=='file-durable': self.bytes['file_read'] += src.size
                    result = private.array
                else:
                    result = src.array
                self.timings['load_s'] += NOW()-start
                start = NOW()
                checked=self.cfg.get('checks','full')=='full'
                pages=src.validate() if checked else None
                replica_pages=private.validate() if private is not None and checked else None
                self.placements.append(dict(key=key, node=node, pages=pages, replica_pages=replica_pages,checked=checked,
                    residency_rechecks=(src.check_retries+(private.check_retries if private is not None else [])) if checked else []))
                self.timings['checks_s'] += NOW()-start
                yield result
            finally:
                del result
                if private is not None: private.close()
                src.close()


def worker(cfg, task, kind, payload, conn, core):
    store = None
    try:
        os.sched_setaffinity(0, {core})
        before = io_counts()
        faults_before = resource.getrusage(resource.RUSAGE_SELF)
        start = NOW()
        store = Store(cfg['control'], task)
        bulk = Bulk(cfg, store, task)
        n, frames = cfg['n'], cfg['frames']
        shape = (frames, 2, n, n)
        result = {}
        compute = NOW()
        if kind=='simulate':
            key, params = payload
            with bulk.output(key, shape) as data:
                simulate(cfg['kernel'], params, n, cfg['steps'], frames, data)
                t = NOW()
                result['trajectory_sha256'] = hashlib.sha256(data).hexdigest()
                bulk.timings['checks_s'] += NOW()-t
            del data
        elif kind=='train':
            dataset, member, round_id, output = payload
            rng = np.random.default_rng(cfg['seed']+1000*round_id+member)
            weights = rng.uniform(.2, 1.8, len(dataset))
            gram = np.zeros((10, 10), dtype=np.float32)
            cross = np.zeros((10, 2*n*n), dtype=np.float32)
            for (key, params), weight in zip(dataset, weights):
                with bulk.read(key, shape) as data:
                    g, c = training_contribution(data, params, weight)
                    gram += g
                    cross += c
                del data
            with bulk.output(output, (10, 2, n, n)) as data:
                data[:] = fit(gram, cross, n)
            del data
        elif kind=='select':
            models, remaining, gold, golden_params = payload
            stride = max(1, n//32)
            points = candidates(cfg['seed'])
            x = np.stack([basis(p, 1) for p in points])
            vx = np.stack([basis(p, 1) for p in golden_params])
            predictions, validation = [], []
            for key in models:
                with bulk.read(key, (10, 2, n, n)) as model:
                    # The full field model was trained. Selection evaluates a
                    # fixed spatial sample; no policy-specific subsampling.
                    sampled = model[:, :, ::stride, ::stride].reshape(10, -1)
                    predictions.append(x @ sampled)
                    validation.append(vx @ sampled)
                del model
            predictions = np.array(predictions)
            score = predictions.var(axis=0).mean(axis=1)
            ranking = sorted(remaining, key=lambda j: (-float(score[j]), j))
            mean = np.mean(validation, axis=0)
            result.update(selected=ranking[:cfg['batch']], uncertainty=float(score[ranking[0]]),
                          validation_nrmse=float(np.linalg.norm(mean-gold)/np.linalg.norm(gold)),
                          prediction_sha256=hashlib.sha256(mean).hexdigest())
        else:
            raise ValueError(kind)
        elapsed = NOW()-compute
        result.update(task=task, kind=kind, elapsed_s=NOW()-start,
                      compute_and_control_s=elapsed-sum(bulk.timings.values()),
                      timings=bulk.timings, bytes=bulk.bytes, placements=bulk.placements,
                      io={k:io_counts()[k]-before[k] for k in before}, pid=os.getpid(),
                      minor_faults=resource.getrusage(resource.RUSAGE_SELF).ru_minflt-faults_before.ru_minflt,
                      major_faults=resource.getrusage(resource.RUSAGE_SELF).ru_majflt-faults_before.ru_majflt)
        conn.send(dict(ok=True, result=result))
    except BaseException:
        conn.send(dict(ok=False, error=traceback.format_exc(), task=task))
    finally:
        if store is not None: store.close()
        conn.close()


def stage(cfg, dsec, epoch, jobs):
    ctx = mp.get_context('fork')
    children = []
    try:
        for i, (name, kind, payload) in enumerate(jobs):
            dsec.admit(name, expected_epoch=epoch)
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=worker, args=(cfg, name, kind, payload, child, cfg['cores'][i]))
            proc.start(); child.close()
            children.append((name, proc, parent))
        rows = []
        for name, proc, pipe in children:
            if not pipe.poll(180): raise TimeoutError(name)
            message = pipe.recv()
            proc.join(10)
            if proc.is_alive() or proc.exitcode!=0 or not message['ok']:
                raise RuntimeError(message)
            # Actual process exit discharges all cached mappings before finish.
            dsec.finish(name, True)
            rows.append(message['result'])
        return rows
    finally:
        for _, proc, pipe in children:
            if proc.is_alive(): proc.terminate()
            proc.join(5); pipe.close()


def contender(path, conn, core):
    os.sched_setaffinity(0, {core})
    buf = Buffer(path, (32*1024*1024,), create=True, node=1)
    buf.array.fill(1)
    pages = buf.validate()
    conn.send(dict(pages=pages, bytes=buf.size))
    start = NOW(); loops = 0
    while not conn.poll():
        np.add(buf.array, .001, out=buf.array)
        loops += 1
    conn.recv()
    conn.send(dict(passes=loops, elapsed_s=NOW()-start, logical_read_write_bytes=loops*buf.size*2))
    buf.close(); conn.close()


def spec(read=(), write=(), deps=()):
    effects = dict(read=list(read), write=list(write))
    return dict(grants=effects, effects=effects, depends_on=list(deps))


def trial(out, config):
    out.mkdir(parents=True, exist_ok=False)
    scratch = Path('/dev/shm')/('science-'+uuid.uuid4().hex)
    scratch.mkdir()
    cfg = dict(config, control=str(out/'control'), cache=str(scratch/'cache'),
               bulk=str(out/'file-data') if config['mode'].startswith('file-') else str(scratch/'bulk'))
    Path(cfg['cache']).mkdir(); Path(cfg['bulk']).mkdir()
    store = None; rival = None; rival_pipe = None
    begin = NOW()
    try:
        store = Store.create(cfg['control'], scratch/'descriptors', capacity=4096)
        trajectory_keys = [f'traj-{i}' for i in range(cfg['initial']+(cfg['rounds']-1)*cfg['batch'])]
        model_keys = [f'model-{r}-{m}' for r in range(cfg['rounds']) for m in range(cfg['members'])]
        for key in trajectory_keys+model_keys: store.seed(key, json.dumps({'kind':'bulk-descriptor','key':key}))
        dsec = DSec.initialize(store, {}, retention_policy=cfg.get('retention', 'dataflow'), session_roots=trajectory_keys)
        with dsec.registry() as state:
            state['bulk'] = dict(budget_bytes=cfg['budget_mib']*1024**2, live_bytes=0, peak_bytes=0, objects={})
        n = cfg['n']; stride = max(1, n//32)
        gp = np.array([[.19, .27], [.73, .61], [.41, .87]])
        gold = []
        for p in gp:
            data = np.empty((cfg['frames'], 2, n, n), np.float32)
            simulate(cfg['kernel'], p, n, cfg['steps'], cfg['frames'], data)
            gold.append(data[-1, :, ::stride, ::stride].reshape(-1))
        del data
        gold = np.array(gold)
        if cfg['contention']:
            parent, child = mp.get_context('fork').Pipe()
            rival = mp.get_context('fork').Process(target=contender, args=(scratch/'rival', child, cfg['cores'][-1]))
            rival.start(); child.close(); rival_pipe = parent
            if not parent.poll(30): raise TimeoutError('contender startup')
            rival_info = parent.recv()
        else: rival_info = None
        setup_s = NOW()-begin
        points = candidates(cfg['seed']); chosen = list(range(cfg['initial']))
        dataset = []; seen = set(); epoch = 0; records = []; tasks = []
        start = NOW()
        for r in range(cfg['rounds']):
            iter_start = NOW()
            new = []
            for j in chosen:
                key = trajectory_keys[len(dataset)]
                dataset.append((key, points[j].tolist())); new.append((key, points[j].tolist())); seen.add(j)
            models = [f'model-{r}-{m}' for m in range(cfg['members'])]
            sim_names = [f'sim-{i}-r{r}' for i in range(len(new))]
            train_names = [f'train-{m}-r{r}' for m in range(cfg['members'])]
            select_name = f'select-r{r}'
            specs = {name:spec(write=[key]) for name,(key,_) in zip(sim_names,new)}
            specs.update({name:spec(read=[key for key,_ in dataset],write=[model],deps=sim_names)
                          for name,model in zip(train_names,models)})
            specs[select_name] = spec(read=models,deps=train_names)
            t = NOW()
            epoch = dsec.replan(specs, expected_epoch=epoch, authorized_new={k:v['grants'] for k,v in specs.items()})
            commit_s = NOW()-t
            t = NOW(); sims = stage(cfg,dsec,epoch,[(name,'simulate',item) for name,item in zip(sim_names,new)])
            simulation_s = NOW()-t
            t = NOW(); trains = stage(cfg,dsec,epoch,[(name,'train',(dataset,m,r,models[m])) for m,name in enumerate(train_names)])
            training_s = NOW()-t
            t = NOW(); selected = stage(cfg,dsec,epoch,[(select_name,'select',(models, sorted(set(range(64))-seen), gold, gp))])
            selection_s = NOW()-t
            chosen = selected[0]['selected']
            with dsec.registry() as state: bulk_state = dict(state['bulk'])
            managed = sum(p.stat().st_size for p in Path(cfg['bulk']).iterdir())
            replicas = sum(p.stat().st_size for p in Path(cfg['cache']).iterdir())
            controls = sum(p.stat().st_size for p in (scratch/'descriptors').rglob('*') if p.is_file())
            t = NOW()
            for key in models:
                dsec.retire(key); (Path(cfg['bulk'])/key).unlink()
                # Delete no-longer-used cached replicas of completed models.
                for p in Path(cfg['cache']).glob('*-'+key): p.unlink()
                with dsec.registry() as state:
                    state['bulk']['live_bytes'] -= state['bulk']['objects'].pop(key)
            reclaim_s = NOW()-t
            record = dict(iteration=r, iteration_s=NOW()-iter_start, commit_s=commit_s,
                          simulation_s=simulation_s,training_s=training_s,selection_s=selection_s,
                          reclaim_s=reclaim_s, dataset_cases=len(dataset), bulk_bytes=managed,
                          replica_bytes=replicas, control_bytes=controls, pool_peak_bytes=bulk_state['peak_bytes'],
                          selected=chosen, validation_nrmse=selected[0]['validation_nrmse'],
                          prediction_sha256=selected[0]['prediction_sha256'])
            records.append(record); tasks.extend(sims+trains+selected)
            with (out/'iterations.jsonl').open('a') as f: f.write(json.dumps(record)+'\n')
        discovery_s = NOW()-start
        t = NOW()
        for key in trajectory_keys:
            dsec.release_root(key); dsec.retire(key)
            (Path(cfg['bulk'])/key).unlink()
            with dsec.registry() as state:
                state['bulk']['live_bytes'] -= state['bulk']['objects'].pop(key)
        dsec.snapshot()
        cleanup_s = NOW()-t
        if rival is not None:
            rival_pipe.send('stop')
            if not rival_pipe.poll(30): raise TimeoutError('contender exit')
            rival_info.update(rival_pipe.recv()); rival.join(10)
        summary = dict(schema='science-loop-v1',status='complete',config=config,setup_s=setup_s,
                       discovery_s=discovery_s,cleanup_s=cleanup_s,iterations=records,tasks=tasks,
                       contender=rival_info,all_bulk_released=True)
        dump(out/'result.json',summary)
        return summary
    finally:
        if rival is not None and rival.is_alive(): rival.terminate(); rival.join(10)
        if rival_pipe is not None: rival_pipe.close()
        if store is not None: store.close()
        shutil.rmtree(scratch)
        if Path(cfg['bulk']).exists(): shutil.rmtree(cfg['bulk'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--suite', choices=['smoke','main','stress','compute','refs','scale','instrumentation'], default='smoke')
    p.add_argument('--repeats', type=int, default=3)
    a = p.parse_args()
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    cores = sorted(os.sched_getaffinity(0))
    base = dict(frames=16, steps=64, rounds=4, initial=4, batch=2, members=2,
                contention=False, seed=712, budget_mib=1024, cores=cores[:8]+[cores[12]],checks='full')
    configs = []
    if a.suite=='smoke':
        for mode in MODES: configs.append(dict(base,kernel='gray-scott',n=64,mode=mode,rounds=2))
    elif a.suite=='main':
        for kernel in ['gray-scott','wave']:
            for n in [128,256]:
                for mode in MODES: configs.append(dict(base,kernel=kernel,n=n,mode=mode))
    elif a.suite=='stress':
        for kernel in ['gray-scott','wave']:
            for members,contention in [(4,False),(2,True),(4,True)]:
                for mode in ['file-mmap','dram-shared','cxl-shared','cxl-cache']:
                    configs.append(dict(base,kernel=kernel,n=256,mode=mode,members=members,contention=contention))
    elif a.suite=='compute':
        for kernel in ['gray-scott','wave']:
            for mode in ['file-mmap','dram-shared','cxl-shared','cxl-cache']:
                configs.append(dict(base,kernel=kernel,n=256,mode=mode,steps=256))
    elif a.suite=='scale':
        for kernel in ['gray-scott','wave']:
            for mode in ['file-mmap','dram-shared','cxl-shared','cxl-cache']:
                configs.append(dict(base,kernel=kernel,n=512,mode=mode,budget_mib=2048))
    elif a.suite=='instrumentation':
        for kernel in ['gray-scott','wave']:
            for mode in ['file-mmap','cxl-shared']:
                for checks in ['full','publication']:
                    configs.append(dict(base,kernel=kernel,n=256,mode=mode,checks=checks))
    else:
        for retention in ['dataflow','refcount']:
            for kernel in ['gray-scott','wave']:
                configs.append(dict(base,kernel=kernel,n=256,mode='cxl-shared',retention=retention))
    manifest = dict(schema='science-suite-v1',suite=a.suite,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                    hardware=hardware(1),numpy=np.__version__,python=sys.version,
                    methods='Paired seeds within randomized complete blocks; no warmup trials discarded.',
                    sources={},host_load=Path('/proc/loadavg').read_text(),meminfo=Path('/proc/meminfo').read_text())
    (out/'source').mkdir()
    for f in list(HERE.glob('*.py'))+[HERE.parent/'agent_sharing'/x for x in ['store.py','dsec.py','cxl_lock.py','dataflow.py']]:
        shutil.copy2(f,out/'source'/f.name); manifest['sources'][f.name]=hashlib.sha256(f.read_bytes()).hexdigest()
    dump(out/'manifest.json',manifest)
    failed = 0
    for rep in range(a.repeats):
        order = configs.copy(); random.Random(1907+rep).shuffle(order)
        for i, config in enumerate(order):
            cfg = dict(config,seed=712+rep,rep=rep)
            name = f'b{rep}-{i:02d}-{cfg["kernel"]}-n{cfg["n"]}-{cfg["mode"]}-m{cfg["members"]}-c{int(cfg["contention"])}'
            try:
                result=trial(out/name,cfg)
                row=dict(trial=name,status='complete',discovery_s=result['discovery_s'],config=cfg)
            except BaseException:
                failed += 1
                row=dict(trial=name,status='failed',config=cfg,error=traceback.format_exc())
                dump(out/(name+'-failure.json'),row)
            with (out/'trials.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
            print(json.dumps(row),flush=True)
    manifest.update(completed_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),trials=len(configs)*a.repeats,failed=failed)
    dump(out/'manifest.json',manifest)
    if failed: raise SystemExit(f'{failed} failed trials; retained in manifest')


if __name__=='__main__': main()
