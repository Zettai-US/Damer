"""Adapter for the existing DSec state machine, backed by CXLMemSim bytes.

Disk files contain an object-name inventory and audit snapshots only. The live
registry is read/written via the server under its CAS lock at pool offset zero.
This is cooperative protocol enforcement, not malicious-client isolation.
"""
import contextlib
import json
import struct
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'agent_sharing'))
from store import Store


class Backend:
    name = 'CXLMemSim server CAS'

    def __init__(self, client):
        self.client = client
        self.active = None

    def acquire(self, key, operation, timeout):
        if key != 'dsec-control' or self.active is not None:
            raise RuntimeError('unexpected registry lock')
        self.active = self.client.lock(0, 1, timeout)
        self.active.__enter__()
        return dict(token=1)

    def release(self, token):
        if token != 1 or self.active is None:
            raise RuntimeError('invalid registry token')
        self.active.__exit__(None, None, None)
        self.active = None


class SimulatorStore(Store):
    def __init__(self, root, client, keys):
        self.root = Path(root)
        self.root.mkdir()
        (self.root/'meta').mkdir()
        for key in keys:
            (self.root/'meta'/(key+'.json')).write_text(json.dumps(dict(key=key)))
        self.client = client
        self.backend = Backend(client)
        self.capacity = 262144

    def record(self, key, initial=None, capacity=262144):
        if key != 'dsec-registry' or capacity != self.capacity:
            raise ValueError('unsupported record')
        if initial is not None:
            initial['state_location'] = 'CXLMemSim TCP pool (simulated CXL)'
            self.write_record(key, initial)

    def read_record(self, key):
        if key != 'dsec-registry':
            raise KeyError(key)
        header = self.client.transfer(4096, size=64)
        length = struct.unpack_from('<Q', header)[0]
        if not 0 < length <= self.capacity-64:
            raise ValueError('invalid registry length')
        return json.loads(self.client.transfer(4160, size=length))

    def write_record(self, key, value):
        if key != 'dsec-registry':
            raise KeyError(key)
        raw = json.dumps(value, separators=(',', ':')).encode()
        if len(raw) > self.capacity-64:
            raise MemoryError('registry capacity')
        self.client.transfer(4160, data=raw)
        self.client.transfer(4096, data=struct.pack('<Q56x', len(raw)))

    def close(self):
        pass
