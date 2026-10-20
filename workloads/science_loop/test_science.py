"""Numerical and actual-CXL integration checks for the bulk adapter."""
import tempfile
import unittest
from pathlib import Path
import numpy as np
from run import Buffer, Bulk, Store, DSec, spec
from kernels import laplace, initial, simulate, basis, training_contribution


class Numerics(unittest.TestCase):
    def test_discrete_fourier_eigenvalue(self):
        n = 64
        x = np.cos(2*np.pi*3*np.arange(n)/n)[:, None]*np.ones((1,n))
        expected = -4*np.sin(np.pi*3/n)**2*x
        np.testing.assert_allclose(laplace(x), expected, atol=1e-13)

    def test_wave_one_step_independent_update(self):
        n = 32; a,b = .3,.7
        u,v = initial(n,'wave')
        acceleration = (.5+a)**2 * laplace(u)
        expected_v = .2*acceleration
        out = np.empty((1,2,n,n),np.float32)
        simulate('wave',[a,b],n,1,1,out)
        np.testing.assert_allclose(out[0,1],expected_v,atol=1e-7)
        np.testing.assert_allclose(out[0,0],u+.2*expected_v,atol=1e-7)

    def test_streamed_training_matches_stacked_system(self):
        rng=np.random.default_rng(17)
        params=[[.1,.4],[.8,.3],[.2,.9]]
        ys=[rng.normal(size=(16,2,32,32)).astype(np.float32) for _ in params]
        xs=[basis(p,np.linspace(1/16,1,16)) for p in params]
        actual=[training_contribution(y,p,1) for y,p in zip(ys,params)]
        x=np.concatenate(xs); y=np.concatenate(ys).reshape(48,-1)
        np.testing.assert_allclose(sum(v[0] for v in actual),x.T@x,rtol=2e-5,atol=2e-5)
        np.testing.assert_allclose(sum(v[1] for v in actual),x.T@y,rtol=2e-5,atol=2e-5)

    def test_fields_remain_finite_and_gray_scott_nonnegative(self):
        for kind in ['gray-scott','wave']:
            for p in [[0,0],[1,1],[0,1],[1,0]]:
                out=np.empty((16,2,32,32),np.float32)
                simulate(kind,p,32,256,16,out)
                self.assertTrue(np.isfinite(out).all())
                if kind=='gray-scott':self.assertGreaterEqual(float(out.min()),0)


class CXLContract(unittest.TestCase):
    def test_budget_failure_and_undeclared_use_leave_no_backing(self):
        with tempfile.TemporaryDirectory() as ctl, tempfile.TemporaryDirectory(dir='/dev/shm') as shm:
            root=Path(shm); (root/'bulk').mkdir(); (root/'cache').mkdir()
            store=Store.create(Path(ctl)/'store',root/'control',capacity=4096)
            try:
                for key in ['large','secret']:store.seed(key,'descriptor')
                dsec=DSec.initialize(store,{'writer':spec(write=['large'])},session_roots=['large','secret'])
                with dsec.registry() as state:
                    state['bulk']=dict(budget_bytes=4096,live_bytes=0,peak_bytes=0,objects={})
                dsec.admit('writer',expected_epoch=0)
                bulk=Bulk(dict(mode='cxl-shared',bulk=str(root/'bulk'),cache=str(root/'cache')),store,'writer')
                with self.assertRaises(MemoryError):
                    with bulk.output('large',(2048,)):self.fail('budget was bypassed')
                with self.assertRaises(PermissionError):
                    with bulk.read('secret',(1024,)):self.fail('effects were bypassed')
                with dsec.registry() as state:
                    self.assertEqual(state['bulk']['live_bytes'],0)
                    self.assertEqual(state['pins'],{})
                self.assertEqual(list((root/'bulk').iterdir()),[])
                dsec.finish('writer',False)
            finally:store.close()


if __name__=='__main__':unittest.main()
