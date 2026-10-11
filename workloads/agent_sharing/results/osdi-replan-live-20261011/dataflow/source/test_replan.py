"""CXL-backed plan transactions: independent authority, liveness, and races."""
import json
import multiprocessing as mp
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from dsec import DSec
from store import Store


def task(reads,depends=(),grants=None):
    return dict(depends_on=list(depends),grants=dict(read=list(reads if grants is None else grants)),
                effects=dict(read=list(reads)))


def race_action(root,key,operation,barrier,queue):
    store=Store(root);dsec=DSec(store)
    try:
        barrier.wait(10)
        if operation=='replan':
            dsec.replan({'reader':task([key])},expected_epoch=0,authorized_new={'reader':{'read':[key]}})
        else:dsec.retire(key)
        queue.put((operation,'ok'))
    except (RuntimeError,ValueError) as exc:queue.put((operation,type(exc).__name__))
    finally:store.close()


class ReplanTests(unittest.TestCase):
    def setUp(self):
        self.temp=Path(tempfile.mkdtemp());self.payload=Path('/dev/shm')/('damer-replan-'+uuid.uuid4().hex)
        self.store=Store.create(self.temp/'store',self.payload,node=1,capacity=4096)
        for key in ['x','y','z']:self.store.seed(key,'source')

    def tearDown(self):
        self.store.close();shutil.rmtree(self.temp);shutil.rmtree(self.payload)

    def state(self):return self.store.read_record('dsec-registry')

    def test_atomic_replan_preserves_running_pin_and_root(self):
        d=DSec.initialize(self.store,{'a':task(['x']),'b':task(['y'],['a'])},'refcount',session_roots=['z'])
        d.admit('a',expected_epoch=0)
        with d.pin('a','x','read'):
            d.replan({'c':task(['x'],['a'])},expected_epoch=0,cancel=['b'],authorized_new={'c':{'read':['x']}})
            self.assertEqual(self.state()['pending_uses'],{'x':2})
            with self.assertRaises(RuntimeError):d.reclaim('x')
            with self.assertRaises(RuntimeError):d.reclaim('z')
            d.reclaim('y')
        d.finish('a',True)
        with self.assertRaises(RuntimeError):d.reclaim('x')
        with self.assertRaisesRegex(ValueError,'stale'):d.admit('c',expected_epoch=0)
        d.admit('c',expected_epoch=1);d.finish('c',True);d.reclaim('x')
        d.release_root('z');d.reclaim('z')
        self.assertEqual(self.state()['pending_uses'],{})

    def test_failed_replans_leave_registry_identical(self):
        d=DSec.initialize(self.store,{'a':task(['x']),'b':task(['y'],['a'])},'dataflow')
        cases=[lambda:d.replan({},expected_epoch=1),
               lambda:d.replan({'a':task(['z'])},expected_epoch=0),
               lambda:d.replan({'a':task(['x'],['b'])},expected_epoch=0),
               lambda:d.replan({},expected_epoch=0,cancel=['a']),
               lambda:d.replan({'new':task(['z'])},expected_epoch=0)]
        for operation in cases:
            before=self.state()
            with self.assertRaises((ValueError,PermissionError)):operation()
            self.assertEqual(before,self.state())

    def test_replan_cannot_change_running_task_or_reintroduce_revoked_right(self):
        d=DSec.initialize(self.store,{'a':task(['x']),'b':task(['y'])},'refcount')
        d.admit('a')
        with self.assertRaisesRegex(ValueError,'unstarted'):d.replan({'a':task(['x'])},expected_epoch=0)
        d.revoke('b','y')
        with self.assertRaises(PermissionError):d.replan({'b':task(['y'])},expected_epoch=0)
        self.assertEqual(self.state()['pending_uses'],{'x':1,'y':1})

    def test_retired_object_cannot_be_resurrected_by_replan(self):
        d=DSec.initialize(self.store,{'a':task(['x'],grants=['x','y'])},'refcount')
        d.reclaim('y');before=self.state()
        with self.assertRaisesRegex(ValueError,'retired'):d.replan({'a':task(['y'])},expected_epoch=0)
        self.assertEqual(before,self.state())
        self.assertFalse((self.payload/'y').exists())

    def test_reference_counts_match_independent_task_scan(self):
        d=DSec.initialize(self.store,{'a':task(['x','y','x']),'b':task(['x'],['a']),'c':task(['z'])},'refcount')
        for name in ['a','c','b']:
            d.admit(name)
            with d.registry() as state:
                for key in ['x','y','z']:
                    expected=sum(t['state'] in ['ready','running'] and key in t['effects']['read'] for t in state['sandboxes'].values())
                    self.assertEqual(expected,state['pending_uses'].get(key,0))
            d.finish(name,True)
        self.assertEqual(self.state()['pending_uses'],{})

    def test_oversized_replan_is_transactional(self):
        d=DSec.initialize(self.store,{'a':task(['x'])},'refcount');before=self.state()
        spec=task(['x']);spec['description']='x'*300000
        with self.assertRaisesRegex(ValueError,'capacity'):d.replan({'a':spec},expected_epoch=0)
        self.assertEqual(before,self.state())

    def test_replan_and_retirement_serialize_on_cxl_lock(self):
        d=DSec.initialize(self.store,{},'refcount')
        ctx=mp.get_context('spawn');barrier=ctx.Barrier(2);queue=ctx.Queue()
        workers=[ctx.Process(target=race_action,args=(self.store.root,'x',op,barrier,queue)) for op in ['retire','replan']]
        try:
            for p in workers:p.start()
            for p in workers:p.join(15);self.assertEqual(p.exitcode,0)
            result=dict(queue.get(timeout=2) for _ in workers)
            self.assertEqual(sum(v=='ok' for v in result.values()),1)
            state=self.state()
            self.assertFalse('x' in state['retired'] and state['pending_uses'].get('x',0)>0)
        finally:
            for p in workers:
                if p.is_alive():p.terminate();p.join()
            queue.close();queue.join_thread()


if __name__=='__main__':unittest.main()
