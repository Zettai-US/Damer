"""Protocol and lifetime checks against a real local CXLMemSim executable."""
import concurrent.futures
import json
from pathlib import Path
import struct
import tempfile
import unittest
from memsim_client import Client
from memsim_store import SimulatorStore
from run_memsim import Server, HERE, spec
from dsec import DSec


class SimulatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='arvm-memsim-test-')
        self.root = Path(self.tmp.name)
        self.server = Server(HERE/'build/memsim/cxlmemsim_server', self.root, 100)
        self.client = self.server.client

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_pipelined_binary_and_version_reuse(self):
        raw = bytes(range(256))*2051+b'partial cache line'
        desc = dict(lock=512,header=576,addr=1024*1024,size=len(raw),version=1)
        self.client.publish(desc,raw,7)
        with self.assertRaises(ValueError):
            self.client.publish(desc,raw,7)
        other = Client(self.server.port)
        try:
            self.assertEqual(other.fetch(desc,8),raw)
            with self.client.lock(desc['lock'],7):
                self.client.transfer(desc['header'],data=bytes(64))
            newer = dict(desc,version=2)
            self.client.publish(newer,raw[::-1],7)
            with self.assertRaises(ValueError):
                other.fetch(desc,8)
            self.assertEqual(other.fetch(newer,8),raw[::-1])
            with self.assertRaises(ValueError):
                other.transfer(other.capacity,size=64)
        finally:
            other.close()

    def test_data_capacity_excludes_server_header(self):
        self.assertEqual(self.client.capacity, self.client.info['cachelines']*64)
        self.assertLess(self.client.capacity, self.client.info['backing_capacity'])
        # The server applies modulo addressing: accepting this line could
        # alias the beginning of the pool instead of rejecting the request.
        with self.assertRaises(ValueError):
            self.client.transfer(self.client.capacity, data=bytes(64))
        with self.assertRaises(ValueError):
            self.client.call(4, self.client.capacity, expected=0, value=1)

    def test_cas_mutex_across_connections(self):
        def increment(token):
            c = Client(self.server.port)
            try:
                for _ in range(20):
                    with c.lock(512,token):
                        old = struct.unpack('<Q',c.call(0,576)[1][:8])[0]
                        c.call(1,576,data=struct.pack('<Q',old+1))
            finally:
                c.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(increment,[10,20]))
        self.assertEqual(struct.unpack('<Q',self.client.call(0,576)[1][:8])[0],40)

    def test_dsec_future_uses_pins_and_stale_epoch(self):
        store = SimulatorStore(self.root/'control',self.client,['field'])
        dsec = DSec.initialize(store,{},retention_policy='dataflow')
        specs = {'consumer':spec(read=['field'])}
        epoch = dsec.replan(specs,expected_epoch=0,authorized_new={'consumer':specs['consumer']['grants']})
        with self.assertRaises(RuntimeError):
            dsec.retire('field')
        with self.assertRaises(ValueError):
            dsec.admit('consumer',epoch-1)
        dsec.admit('consumer',epoch)
        with self.assertRaises(PermissionError):
            with dsec.pin('consumer','field','write'):
                pass
        with dsec.pin('consumer','field','read'):
            with self.assertRaises(RuntimeError):
                dsec.finish('consumer',True)
            with self.assertRaises(RuntimeError):
                dsec.retire('field')
        dsec.finish('consumer',True)
        dsec.retire('field')
        with dsec.registry() as state:
            self.assertFalse(state['pins'])
            self.assertFalse(state['pending_uses'])


if __name__=='__main__':
    unittest.main()
