#!/usr/bin/env python3
"""GPU integration checks, including an independent existing NumPy oracle."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

BINARY = str(Path(sys.argv.pop(1)).resolve())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'science_loop'))
from kernels import simulate


def run(*args, ok=True):
    p = subprocess.run([BINARY, *map(str, args)], capture_output=True, text=True, timeout=30)
    if ok and p.returncode:
        raise AssertionError(p.stdout + p.stderr)
    return p, json.loads(p.stdout) if p.stdout.strip() else None


class Runtime(unittest.TestCase):
    def test_wave_against_existing_numpy_kernel_and_modes(self):
        for n, steps in [(17, 1), (32, 16), (33, 64)]:
            expected = np.empty((1, 2, n, n), dtype=np.float32)
            simulate('wave', [.3, .7], n, steps, 1, expected)
            outputs = []
            for mode in ['persistent', 'host', 'graph']:
                with self.subTest(n=n, steps=steps, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'output.f32'
                    _, result = run('--n', n, '--steps', steps, '--repeats', 3,
                                    '--mode', mode, '--slots', 2, '--output', path)
                    actual = np.fromfile(path, np.float32).reshape(2, n, n)
                    np.testing.assert_allclose(actual, expected[0], atol=2e-6, rtol=2e-5)
                    self.assertTrue(result['valid'])
                    self.assertEqual(result['completed_tasks'], steps*3)
                    self.assertEqual(result['retirements'], steps*3)
                    self.assertEqual(result['peak_slots'], 2)
                    self.assertEqual(result['host_submission_calls'], (steps+2)*3 if mode == 'host' else 1)
                    outputs.append(actual)
            for actual in outputs[1:]:
                np.testing.assert_array_equal(actual, outputs[0])

    def test_non_topological_fanout_join(self):
        for mode in ['persistent', 'host', 'graph']:
            with self.subTest(mode=mode):
                _, r = run('--workload', 'diamond', '--mode', mode, '--repeats', 5)
                self.assertTrue(r['valid'])
                self.assertEqual(r['peak_slots'], 3)
                self.assertEqual(r['completed_tasks'], 15)

    def test_fail_closed_device_faults(self):
        for mode in ['persistent', 'host', 'graph']:
            for fault, code in [('capacity', 2), ('uses', 3), ('nan', 4)]:
                with self.subTest(mode=mode, fault=fault):
                    proc, r = run('--fault', fault, '--mode', mode, '--repeats', 1, ok=False)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertFalse(r['valid'])
                    self.assertEqual(r['error_code'], code)
                    self.assertEqual(r['publications'], 0)
                    self.assertEqual(r['completed_runs'], 0)

    def test_reject_cycle_before_execution(self):
        p, r = run('--fault', 'cycle', ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn('cycle', p.stderr)
        self.assertIsNone(r)

    def test_invalid_arguments(self):
        for args in [('--n', '0'), ('--steps', '-1'), ('--mode', 'gin'),
                     ('--slots', '0'), ('--n', '32junk'), ('--fault', 'unknown')]:
            with self.subTest(args=args):
                p, _ = run(*args, ok=False)
                self.assertEqual(p.returncode, 1)


if __name__ == '__main__':
    unittest.main()
