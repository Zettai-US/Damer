import random
import threading
import unittest
from runtime import Registry, Compiler, Task, Handle


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.r=Registry(1024); self.h=self.r.publish('passage',256,{'a'})
        self.c=Compiler(self.r)
        self.tasks=[Task('read',(),(self.h,),128)]

    def test_unauthorized_reader(self):
        with self.assertRaises(PermissionError): self.c.compile(self.tasks,'b')

    def test_unknown_generation(self):
        with self.assertRaises(ValueError): self.c.compile([Task('read',(),(Handle('passage',99),),1)],'a')

    def test_recycled_name_keeps_generations_distinct(self):
        new=self.r.publish('passage',512,{'a'})
        self.assertNotEqual(new,self.h)
        self.assertEqual(self.r.validate(self.h,'a').size,256)
        self.assertEqual(self.r.validate(new,'a').size,512)

    def test_retirement_waits_for_reader_and_root(self):
        lease=self.r.admit(self.c.compile(self.tasks,'a'))
        self.r.retire(self.h)
        with self.assertRaises(RuntimeError):self.r.reclaim(self.h)
        self.r.drop_root(self.h)
        with self.assertRaises(RuntimeError):self.r.reclaim(self.h)
        self.r.finish(lease);self.r.reclaim(self.h)
        with self.assertRaises(ValueError):self.r.validate(self.h,'a')

    def test_capacity_rejection_is_atomic(self):
        c=self.c.compile([Task('big',(),(self.h,),1025)],'a')
        with self.assertRaises(MemoryError):self.r.admit(c)
        self.assertEqual(self.r.reserved,0);self.assertEqual(self.r.objects[self.h].pins,0)

    def test_duplicate_references_pin_backing_once(self):
        tasks=[Task('a',(),(self.h,),1),Task('b',('a',),(self.h,),1)]
        lease=self.r.admit(self.c.compile(tasks,'a'))
        self.assertEqual(self.r.objects[self.h].pins,1)
        self.r.finish(lease);self.assertEqual(self.r.objects[self.h].pins,0)

    def test_stale_epoch_rejected(self):
        c=self.c.compile(self.tasks,'a');self.r.publish('other',1,{'a'})
        with self.assertRaises(ValueError):self.r.admit(c)

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):self.c.compile([Task('a',('b',),(),1),Task('b',('a',),(),1)],'a')

    def test_missing_dependency_rejected(self):
        with self.assertRaises(ValueError):self.c.compile([Task('a',('missing',),(),1)],'a')

    def test_incremental_bound_changes_admission(self):
        self.c.compile(self.tasks,'a');c=self.c.update_bounds({'read':1025})
        with self.assertRaises(MemoryError):self.r.admit(c)
        c=self.c.update_bounds({'read':64});lease=self.r.admit(c)
        self.assertEqual(self.r.reserved,64);self.r.finish(lease)

    def test_double_completion_rejected(self):
        lease=self.r.admit(self.c.compile(self.tasks,'a'));self.r.finish(lease)
        with self.assertRaises(KeyError):self.r.finish(lease)
        self.assertEqual(self.r.reserved,0)

    def test_retire_races_with_admission(self):
        for _ in range(100):
            r=Registry(1024);h=r.publish('x',8,{'a'});c=Compiler(r).compile([Task('t',(),(h,),16)],'a')
            start=threading.Barrier(2);leases=[]
            def admit():
                start.wait()
                try:leases.append(r.admit(c))
                except ValueError:pass
            t=threading.Thread(target=admit);t.start();start.wait();r.retire(h);t.join();r.drop_root(h)
            if leases:
                with self.assertRaises(RuntimeError):r.reclaim(h)
                r.finish(leases[0])
            r.reclaim(h);self.assertFalse(r.objects[h].live)

    def test_randomized_admit_complete_sequences(self):
        for seed in range(100):
            rng=random.Random(seed);r=Registry(1024);h=r.publish('x',32,{'a'});compiler=Compiler(r);active=[]
            for _ in range(200):
                if active and rng.random()<.45:r.finish(active.pop(rng.randrange(len(active))))
                else:
                    c=compiler.compile([Task('t',(),(h,),rng.choice([0,64,256,512,1025]))],'a')
                    try:active.append(r.admit(c))
                    except MemoryError:pass
                self.assertTrue(0<=r.reserved<=1024)
                self.assertEqual(r.objects[h].pins,len(active))
                self.assertEqual(r.reserved,sum(r.active[x].reservation_bytes for x in active))
            for lease in active:r.finish(lease)
            self.assertEqual(r.reserved,0);self.assertEqual(r.objects[h].pins,0)


if __name__=='__main__':unittest.main(verbosity=2)
