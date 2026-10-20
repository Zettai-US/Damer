#!/usr/bin/env python3
"""Three-machine closed-loop experiments using an actual CXLMemSim server.

All measured wall times include simulator execution and SSH/TCP transport.
They are functional-simulator E2E results, not hardware CXL performance claims.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
import argparse
from contextlib import ExitStack
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import random
import selectors
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import time
import traceback
import uuid
import numpy as np
from kernels import simulate, candidates, basis
from memsim_client import Client
from memsim_store import SimulatorStore
from dsec import DSec

HERE = Path(__file__).resolve().parent
SSH = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ForwardAgent=no', '-o',
       'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o',
       'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'LogLevel=ERROR']
SOURCES = ['kernels.py', 'memsim_client.py', 'memsim_worker.py', 'memsim_store.py', 'run_memsim.py']
NOW = time.perf_counter


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2)+'\n')


def command(args):
    return subprocess.check_output(args, text=True).strip()


def deploy(host, remote):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tf:
        for name in ['kernels.py', 'memsim_client.py', 'memsim_worker.py']:
            tf.add(HERE/name, arcname=name)
    subprocess.run(SSH+[host, 'mkdir -m 700 '+shlex.quote(remote)+' && tar -xf - -C '+shlex.quote(remote)],
                   input=archive.getvalue(), check=True, timeout=30)


class Remote:
    def __init__(self, host, remote, port, log):
        self.log = open(log, 'w')
        # The remote port is kernel allocated; obtain it from a short helper.
        script = "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1])"
        remote_port = int(command(SSH+[host, 'python3 -c '+shlex.quote(script)]))
        forwarding = '127.0.0.1:{}:127.0.0.1:{}'.format(remote_port, port)
        cmd = 'cd {} && exec python3 -u memsim_worker.py --port {}'.format(shlex.quote(remote), remote_port)
        self.proc = subprocess.Popen(SSH+['-o','ExitOnForwardFailure=yes','-R',forwarding,host,cmd],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                     text=True, bufsize=1)
        self.host = host
        self.hello = self.receive(30)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message)+'\n')
        self.proc.stdin.flush()

    def receive(self, timeout=240):
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                raise TimeoutError('remote worker '+self.host)
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError('remote worker '+self.host)
        message = json.loads(line)
        if not message.get('ok'):
            raise RuntimeError(message)
        return message

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(10)
        self.log.close()


class Server:
    def __init__(self, binary, out, latency):
        self.backing = '/dev/shm/arvm-memsim-'+uuid.uuid4().hex
        self.log = (out/'server.log').open('w')
        (out/'topology.txt').write_text('(1);\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.args = [str(binary), '--comm-mode=tcp', '--port='+str(self.port), '--capacity=64',
                     '--default_latency='+str(latency), '--backing-file='+self.backing,
                     '--topology='+str(out/'topology.txt')]
        self.proc = subprocess.Popen(self.args, stdout=self.log, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, CXL_BASE_ADDR='0', SPDLOG_LEVEL='warn'))
        deadline = time.monotonic()+30
        while True:
            try:
                self.client = Client(self.port)
                break
            except (ConnectionRefusedError, OSError):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self.close()
                    raise RuntimeError('CXLMemSim startup failed')
                time.sleep(.05)

    def close(self):
        if hasattr(self, 'client'):
            self.client.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.log.close()
        Path(self.backing).unlink(missing_ok=True)


def spec(read=(), write=(), deps=()):
    effects = dict(read=list(read), write=list(write))
    return dict(grants=effects, effects=effects, depends_on=list(deps))


def trial(out, cfg, binary, remote_dir, hosts, check_locks=False):
    out.mkdir()
    begin = NOW()
    with ExitStack() as resources:
        server = Server(binary, out, cfg['latency_ns'])
        resources.callback(server.close)
        client = server.client
        remotes = {}
        for host in hosts:
            worker = Remote(host, remote_dir, server.port, out/(host+'.stderr'))
            resources.callback(worker.close)
            remotes[host] = worker
        host_rows = {h:r.hello for h,r in remotes.items()}
        if len({r['inventory']['machine_id'] for r in host_rows.values()}) != len(hosts):
            raise RuntimeError('worker aliases resolve to the same OS instance')
        for row in host_rows.values():
            for name, sha in row['inventory']['source_hashes'].items():
                if hashlib.sha256((HERE/name).read_bytes()).hexdigest() != sha:
                    raise ValueError('remote source mismatch')
        rtt = {}
        for host, worker in remotes.items():
            samples = []
            for _ in range(3):
                t = NOW(); worker.send(dict(op='ping')); worker.receive()
                samples.append(NOW()-t)
            rtt[host] = samples
        lock_test = None
        if check_locks:
            client.call(1, 576, data=struct.pack('<Q', 0))
            for i, worker in enumerate(remotes.values()):
                worker.send(dict(op='counter', count=25, token=200+i))
            lock_test = [r.receive() for r in remotes.values()]
            count = struct.unpack('<Q', client.call(0, 576)[1][:8])[0]
            if count != 25*len(remotes):
                raise AssertionError('cross-host CAS lost an update')
        traj_keys = ['traj-'+str(i) for i in range(10)]
        model_keys = ['model-{}-{}'.format(r,m) for r in range(4) for m in range(2)]
        store = SimulatorStore(out/'control', client, traj_keys+model_keys)
        dsec = DSec.initialize(store, {}, retention_policy='dataflow', session_roots=traj_keys)
        n = cfg['n']; shape = [16,2,n,n]
        cursor = 1024*1024
        descriptors = {}
        for key in traj_keys+model_keys[:2]:
            s = shape if key.startswith('traj') else [10,2,n,n]
            size = int(np.prod(s))*4
            descriptors[key] = dict(key=key, version=1, lock=cursor, header=cursor+64, addr=cursor+128, size=size, shape=s)
            cursor += 128+size
        for r in range(1,4):
            for m in range(2):
                key = 'model-{}-{}'.format(r,m)
                descriptors[key] = dict(descriptors['model-0-'+str(m)],key=key,version=r+1)
        if cursor > client.capacity-4096:
            raise MemoryError('simulated pool capacity exceeded')
        gp = np.array([[.19,.27],[.73,.61],[.41,.87]])
        stride = max(1, n//32)
        gold = []
        for p in gp:
            data = np.empty(shape,np.float32)
            simulate(cfg['kernel'],p,n,64,16,data)
            gold.append(data[-1,:,::stride,::stride].reshape(-1))
        gold = np.array(gold)
        del data
        points = candidates(cfg['seed']); chosen = list(range(4)); seen = set()
        dataset = []; epoch = 0; records = []; simulations = []
        assignments = {hosts[0]:[0,1]} if cfg['layout'] != 'split' else {hosts[0]:[0], hosts[1]:[1]}
        setup_s = NOW()-begin
        before_protocol = dict(client.stats)
        start = NOW()
        for r in range(4):
            iteration_start = NOW()
            new = []
            for idx in chosen:
                key = traj_keys[len(dataset)]
                desc = dict(descriptors[key], params=points[idx].tolist())
                dataset.append(desc); new.append(desc); seen.add(idx)
            sim_tasks = ['sim-{}-{}'.format(r,i) for i in range(len(new))]
            train_tasks = ['train-{}-{}'.format(r,m) for m in range(2)]
            select_task = 'select-'+str(r)
            models = {str(m):descriptors['model-{}-{}'.format(r,m)] for m in range(2)}
            specs = {task:spec(write=[desc['key']]) for task,desc in zip(sim_tasks,new)}
            specs.update({task:spec(read=[d['key'] for d in dataset], write=[models[str(m)]['key']], deps=sim_tasks)
                          for m,task in enumerate(train_tasks)})
            specs[select_task] = spec(read=[d['key'] for d in models.values()], deps=train_tasks)
            t = NOW()
            epoch = dsec.replan(specs, expected_epoch=epoch, authorized_new={k:v['grants'] for k,v in specs.items()})
            commit_s = NOW()-t
            t = NOW()
            for task, desc in zip(sim_tasks,new):
                dsec.admit(task, epoch)
                with dsec.pin(task, desc['key'], 'write'):
                    data = np.empty(shape, np.float32)
                    compute_start = NOW()
                    simulate(cfg['kernel'],desc['params'],n,64,16,data)
                    compute_s = NOW()-compute_start
                    pub = NOW(); digest = client.publish(desc,data.tobytes(),10)
                    simulations.append(dict(key=desc['key'],sha256=digest,compute_s=compute_s,publish_s=NOW()-pub))
                    del data
                dsec.finish(task, True)
            simulation_s = NOW()-t
            t = NOW()
            with ExitStack() as pins:
                for m,task in enumerate(train_tasks):
                    dsec.admit(task, epoch)
                    for desc in dataset:
                        pins.enter_context(dsec.pin(task, desc['key'], 'read'))
                    pins.enter_context(dsec.pin(task, models[str(m)]['key'], 'write'))
                dispatch_start = NOW()
                for host, members in assignments.items():
                    remotes[host].send(dict(op='train',config=cfg,round=r,dataset=dataset,models=models,members=members))
                replies = [remotes[host].receive() for host in assignments]
                remote_stage_s = NOW()-dispatch_start
            for task in train_tasks:
                dsec.finish(task, True)
            training_s = NOW()-t
            t = NOW()
            dsec.admit(select_task, epoch)
            preds = []; vals = []; model_hashes = []
            x = np.stack([basis(p,1) for p in points]); vx = np.stack([basis(p,1) for p in gp])
            for desc in models.values():
                with dsec.pin(select_task, desc['key'], 'read'):
                    raw = client.fetch(desc,11)
                    model_hashes.append(hashlib.sha256(raw).hexdigest())
                    model = np.frombuffer(raw,np.float32).reshape(desc['shape'])
                    sample = model[:,:,::stride,::stride].reshape(10,-1)
                    preds.append(x@sample); vals.append(vx@sample)
                    del model,sample,raw
            scores = np.array(preds).var(axis=0).mean(axis=1)
            chosen = sorted(set(range(64))-seen,key=lambda j:(-float(scores[j]),j))[:2]
            mean = np.mean(vals,axis=0)
            np.save(out/('validation-r'+str(r)+'.npy'),mean)
            np.save(out/('scores-r'+str(r)+'.npy'),scores)
            dsec.finish(select_task,True)
            selection_s = NOW()-t
            t = NOW()
            for desc in models.values():
                dsec.retire(desc['key'])
                # The two model slots are reused only after all consumers drain.
                # Clearing the header invalidates stale version descriptors.
                with client.lock(desc['lock'],12):
                    client.transfer(desc['header'],data=bytes(64))
            reclaim_s = NOW()-t
            row = dict(iteration=r,iteration_s=NOW()-iteration_start,commit_s=commit_s,simulation_s=simulation_s,
                       training_s=training_s,remote_stage_s=remote_stage_s,selection_s=selection_s,reclaim_s=reclaim_s,
                       dataset_cases=len(dataset),selected=chosen,validation_nrmse=float(np.linalg.norm(mean-gold)/np.linalg.norm(gold)),
                       prediction_sha256=hashlib.sha256(mean).hexdigest(),model_sha256=model_hashes,workers=replies,
                       live_payload_bytes=sum(d['size'] for d in dataset)+sum(d['size'] for d in models.values()))
            records.append(row)
            with (out/'iterations.jsonl').open('a') as f:
                f.write(json.dumps(row)+'\n')
        discovery_s = NOW()-start
        measured_protocol = {k:client.stats[k]-before_protocol[k] for k in before_protocol}
        t = NOW()
        for remote in remotes.values():
            remote.send(dict(op='reset'))
            if remote.receive()['cache_bytes'] != 0:
                raise AssertionError('worker did not release its cache')
        for key in traj_keys:
            dsec.release_root(key); dsec.retire(key)
        dsec.snapshot()
        with dsec.registry() as state:
            if state['pins'] or state['pending_uses'] or state['session_roots']:
                raise AssertionError('DSec did not drain')
        result = dict(schema='science-memsim-v1',status='complete',config=cfg,setup_s=setup_s,
                      discovery_s=discovery_s,cleanup_s=NOW()-t,iterations=records,simulations=simulations,
                      coordinator_protocol=measured_protocol,hosts=host_rows,control_round_trip_s=rtt,
                      lock_test=lock_test,server_args=server.args,pool=client.info,
                      allocated_address_bytes=cursor,all_pins_roots_and_remote_caches_drained=True,
                      timing_scope='actual simulator + SSH/TCP + numerical work; not hardware prediction')
        dump(out/'result.json',result)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--server',type=Path,default=HERE/'build/memsim/cxlmemsim_server')
    parser.add_argument('--memsim-source',type=Path,default=Path('/root/CXLMemSim'))
    parser.add_argument('--hosts',nargs=2,default=['bana','6787p'])
    parser.add_argument('--suite',choices=['smoke','main','placement'],default='smoke')
    parser.add_argument('--repetitions',type=int,default=5)
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    (out/'source').mkdir()
    sources = {}
    for name in SOURCES:
        shutil.copy2(HERE/name,out/'source'/name)
        sources[name] = hashlib.sha256((HERE/name).read_bytes()).hexdigest()
    for name in ['dsec.py','dataflow.py','store.py']:
        shutil.copy2(HERE.parent/'agent_sharing'/name,out/'source'/name)
        sources[name] = hashlib.sha256((out/'source'/name).read_bytes()).hexdigest()
    server_sources = {str(p.relative_to(args.memsim_source)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for d in ('src','include') for p in (args.memsim_source/d).rglob('*') if p.is_file()}
    manifest = dict(suite=args.suite,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),sources=sources,
                    server_sha256=hashlib.sha256(args.server.read_bytes()).hexdigest(),server_sources=server_sources,
                    server_commit=command(['git','-C',str(args.memsim_source),'rev-parse','HEAD']),
                    server_build_flags='Release; microbenchmarks OFF; SlugAllocator OFF; RDMA OFF',
                    coordinator=dict(host=socket.gethostname(),python=sys.version,numpy=np.__version__,
                                     machine_id=Path('/etc/machine-id').read_text().strip(),
                                     cores=sorted(os.sched_getaffinity(0)),kernel=os.uname().release),
                    hosts=args.hosts,repetitions=args.repetitions,
                    random_order_seed_base=2911,model='default_latency=100 ns; GFAM/DCD disabled; capacity=64 MiB')
    dump(out/'manifest.json',manifest)
    remote_dir = '/tmp/arvm-science-'+uuid.uuid4().hex
    deployed = []
    try:
        for host in args.hosts:
            deploy(host,remote_dir); deployed.append(host)
        configs = []
        if args.suite == 'smoke':
            configs = [dict(kernel='wave',n=32,policy=p,layout='split') for p in ['refetch','retain','statistics']]
        elif args.suite == 'main':
            configs = [dict(kernel=k,n=n,policy=p,layout='split') for k in ['gray-scott','wave']
                       for n in [32,64] for p in ['refetch','retain','statistics']]
        else:
            configs = [dict(kernel=k,n=64,policy='retain',layout=h) for k in ['gray-scott','wave'] for h in args.hosts]
        manifest['planned_trials'] = len(configs)*args.repetitions
        dump(out/'manifest.json',manifest)
        for rep in range(args.repetitions):
            order = list(configs); random.Random(2911+rep).shuffle(order)
            for i,c in enumerate(order):
                cfg = dict(c,rep=rep,seed=712+rep,latency_ns=100)
                name = 'b{}-{:02d}-{}-n{}-{}-{}'.format(rep,i,c['kernel'],c['n'],c['policy'],c['layout'])
                host_order = args.hosts if c['layout']=='split' else [c['layout']]
                print(json.dumps(dict(event='start',trial=name)),flush=True)
                try:
                    result = trial(out/name,cfg,args.server,remote_dir,host_order,
                                   check_locks=args.suite=='smoke')
                    row = dict(status='complete',trial=name,discovery_s=result['discovery_s'])
                except BaseException:
                    row = dict(status='failed',trial=name,error=traceback.format_exc())
                    dump(out/(name+'-failure.json'),row)
                    with (out/'trials.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
                    raise
                with (out/'trials.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
                print(json.dumps(row),flush=True)
        manifest['completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        dump(out/'manifest.json',manifest)
    finally:
        # Only the unique directory created by this run is removed.
        for host in deployed:
            subprocess.run(SSH+[host,'rm -rf -- '+shlex.quote(remote_dir)],timeout=30,check=False)


if __name__=='__main__':
    main()
