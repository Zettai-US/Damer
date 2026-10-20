"""Fixed-stage baseline must remain safe for the declared static workflow."""
from copy import deepcopy
import unittest
from barrier_control import BarrierController,stage_key

class MemoryStore:
    def record(self,key,initial,capacity):self.value=deepcopy(initial)

class BarrierTest(unittest.TestCase):
    def test_physical_and_simulator_stage_names(self):
        for a,b in [('sim-2-r1','sim-1-2'),('train-0-r3','train-3-0'),('select-r2','select-2')]:
            self.assertEqual(stage_key(a),stage_key(b))

    def test_stage_order_and_manual_retirement(self):
        ctrl=BarrierController.initialize(MemoryStore(),{},session_roots=['traj-0'])
        ctrl.replan({'sim-0-0':{},'train-0-0':{},'train-0-1':{},'select-0':{}},0)
        with self.assertRaises(RuntimeError):ctrl.admit('train-0-0')
        with self.assertRaises(RuntimeError):ctrl.retire('traj-0')
        ctrl.admit('sim-0-0');ctrl.finish('sim-0-0',True)
        ctrl.admit('train-0-0');ctrl.admit('train-0-1');ctrl.finish('train-0-0',True)
        with self.assertRaises(RuntimeError):ctrl.admit('select-0')
        with self.assertRaises(RuntimeError):ctrl.retire('model-0-0')
        with self.assertRaises(ValueError):ctrl.replan({'select-1':{}},1)
        ctrl.finish('train-0-1',True);ctrl.admit('select-0');ctrl.finish('select-0',True)
        ctrl.retire('model-0-0');ctrl.release_root('traj-0');ctrl.retire('traj-0')
        with self.assertRaises(ValueError):ctrl.retire('traj-0')

if __name__=='__main__':unittest.main()
