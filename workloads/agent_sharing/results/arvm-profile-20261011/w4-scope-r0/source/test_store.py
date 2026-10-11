#!/usr/bin/env python3
"""Concurrency and integrity tests on actual NUMA mappings (Linux required)."""
import json
import multiprocessing as mp
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid

from store import Store
from dsec import DSec, SandboxStore
from mcp_server import dispatch
from analyze import analyze_events
from dataflow import reconstruct, compile_memory_contract


def append_worker(root, agent, ready):
    store = Store(root, agent)
    ready.wait(timeout=10)
    for i in range(20):
        store.access("ledger", "append", text=f"{agent}:{i}\n")
    store.close()


def hold_then_exit(root, conn):
    store = Store(root)
    store.backend.acquire("ledger", "write", 1)
    conn.send("held")
    time.sleep(20)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.payload = Path("/dev/shm") / ("damer-test-"+uuid.uuid4().hex)
        self.store = Store.create(self.temp/"store", self.payload, node=1, capacity=4096)
        self.store.seed("ledger", "")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp)
        shutil.rmtree(self.payload)

    def test_concurrent_updates_and_trace_lineage(self):
        ctx=mp.get_context("spawn")
        barrier=ctx.Barrier(4)
        workers=[ctx.Process(target=append_worker,args=(self.store.root,f"agent-{i}",barrier)) for i in range(4)]
        try:
            for w in workers:w.start()
            for w in workers:
                w.join(20)
                self.assertEqual(w.exitcode,0)
            r=self.store.access("ledger")
            self.assertEqual(r["version"],80)
            self.assertEqual(set(r["text"].splitlines()),{f"agent-{a}:{i}" for a in range(4) for i in range(20)})
            events=[json.loads(line) for p in (self.store.root/"events").glob("*.jsonl") for line in p.read_text().splitlines()]
            self.assertEqual(analyze_events(events)["multiwriter_objects"],1)
        finally:
            for w in workers:
                if w.is_alive():w.terminate()
                w.join(5)

    def test_cas_conflict_preserves_committed_value(self):
        self.store.access("ledger","write",text="first",expected_version=0)
        self.assertEqual(self.store.access("ledger","write",text="stale",expected_version=0)["error"],"version_conflict")
        self.assertEqual(self.store.access("ledger")["text"],"first")

    def test_lock_timeout_and_process_death_fail_closed(self):
        ctx=mp.get_context("spawn")
        a,b=ctx.Pipe()
        worker=ctx.Process(target=hold_then_exit,args=(self.store.root,b))
        worker.start()
        try:
            self.assertTrue(a.poll(10))
            self.assertEqual(a.recv(),"held")
            with self.assertRaises(TimeoutError):
                self.store.access("ledger",timeout_s=.01)
        finally:
            worker.terminate()
            worker.join(5)
            a.close();b.close()
        # Upstream SpinLock is not robust: dead owners are never force-unlocked.
        with self.assertRaises(TimeoutError):self.store.access("ledger",timeout_s=.01)

    def test_corruption_fails_closed_and_lock_released(self):
        with (self.payload/"ledger").open("r+b") as f:f.write(b"bad")
        with self.assertRaisesRegex(RuntimeError,"checksum"):
            self.store.access("ledger")
        lock=self.store.backend
        grant=lock.acquire("ledger","write",.1)
        lock.release(grant["token"])

    def test_mcp_roundtrip_and_path_validation(self):
        self.assertIn("tools", dispatch(self.store,"tools/list",{}))
        r=dispatch(self.store,"tools/call",{"name":"append","arguments":{"object":"ledger","text":"ok"}})
        self.assertFalse(r["isError"])
        r=dispatch(self.store,"tools/call",{"name":"read","arguments":{"object":"../ledger"}})
        self.assertTrue(r["isError"])

    def test_grants_dependencies_and_retention(self):
        dsec=DSec.initialize(self.store,{"producer":dict(grants={"write":["ledger"]}),
             "consumer":dict(depends_on=["producer"],grants={"read":["ledger"]})})
        with self.assertRaises(RuntimeError):dsec.admit("consumer")
        with self.assertRaises(RuntimeError):dsec.retire("ledger")
        dsec.admit("producer")
        client=SandboxStore(self.store.root,"producer")
        try:
            with self.assertRaises(PermissionError):client.access("ledger")
            client.access("ledger","write",text="published",expected_version=0)
        finally:client.close()
        dsec.finish("producer",True)
        dsec.admit("consumer")
        with dsec.pin("consumer","ledger","read"):
            with self.assertRaises(RuntimeError):dsec.finish("consumer",True)
        dsec.finish("consumer",True)
        dsec.retire("ledger")

    def test_revoke_drains_existing_pin_and_denies_new_access(self):
        dsec=DSec.initialize(self.store,{"reader":dict(grants={"read":["ledger"]})})
        dsec.admit("reader")
        with dsec.pin("reader","ledger","read"):
            dsec.revoke("reader","ledger")
            with self.assertRaises(PermissionError):
                with dsec.pin("reader","ledger","read"):pass
            with self.assertRaises(RuntimeError):dsec.finish("reader",True)
        dsec.grant("reader","ledger",["read"])
        with dsec.pin("reader","ledger","read"):pass
        dsec.finish("reader",True)

    def test_versioned_dataflow_keeps_distinct_versions(self):
        self.store.trace_kind="opencode-live"
        self.store.agent="writer"
        self.store.access("ledger","write",text="first",expected_version=0)
        self.store.agent="reader-a";self.store.access("ledger")
        self.store.agent="reader-b";self.store.access("ledger")
        self.store.agent="writer";self.store.access("ledger","write",text="second",expected_version=1)
        self.store.agent="reader-a";self.store.access("ledger")
        events=[json.loads(line) for p in (self.store.root/"events").glob("*.jsonl") for line in p.read_text().splitlines()]
        report=reconstruct(events)
        self.assertEqual(report["metrics"]["shared_versions"],1)
        v1=next(v for v in report["versions"] if v["version"]==1)
        self.assertEqual(v1["readers"],["reader-a","reader-b"])
        broken=[dict(e) for e in events]
        next(e for e in broken if e["agent"]=="reader-b")["producer"]="missing-writer"
        with self.assertRaises(ValueError):reconstruct(broken)

    def test_contract_derives_sharing_and_publication_dependency(self):
        tasks={"a":dict(grants={"read":["evidence"],"write":["out"]}),
               "b":dict(depends_on=["a"],grants={"read":["evidence","out"]})}
        c=compile_memory_contract(tasks,{"evidence":0,"out":0})
        self.assertEqual(c["data_dependencies"],[dict(producer="a",consumer="b",object="out",type="publication_dependency")])
        self.assertEqual(c["objects"][0]["retention_until_tasks_complete"],["a","b"])
        tasks["a"]["depends_on"]=["b"]
        with self.assertRaises(ValueError):compile_memory_contract(tasks,{"evidence":0,"out":0})

    def test_dataflow_reclaims_authorized_but_unneeded_state(self):
        self.store.seed("scratch","temporary")
        tasks={"producer":dict(grants={"read":["ledger","scratch"]},effects={"read":["scratch"]}),
            "later":dict(depends_on=["producer"],grants={"read":["ledger","scratch"]},effects={"read":["ledger"]})}
        contract=compile_memory_contract(tasks,{"ledger":0,"scratch":0})
        scratch=next(o for o in contract["objects"] if o["object"]=="scratch")
        self.assertEqual(scratch["readers"],["producer"])
        dsec=DSec.initialize(self.store,tasks,retention_policy="dataflow")
        dsec.admit("producer");dsec.finish("producer",True)
        self.assertEqual(dsec.reclaim("scratch"),4096)
        self.assertFalse((self.payload/"scratch").exists())
        with self.assertRaises(RuntimeError):dsec.reclaim("ledger")
        dsec.admit("later")
        client=SandboxStore(self.store.root,"later")
        try:
            with self.assertRaises(PermissionError):client.access("scratch")
            self.assertEqual(client.access("ledger")["version"],0)
        finally:client.close()
        dsec.finish("later",True);dsec.reclaim("ledger")

    def test_scope_retention_is_conservative_and_roots_survive(self):
        self.store.seed("scratch","temporary")
        dsec=DSec.initialize(self.store,{"later":dict(grants={"read":["ledger","scratch"]},effects={"read":["ledger"]})},
                             retention_policy="scope",session_roots=["ledger"])
        with self.assertRaises(RuntimeError):dsec.reclaim("scratch")
        dsec.admit("later")
        client=SandboxStore(self.store.root,"later")
        try:
            with self.assertRaisesRegex(PermissionError,"accepted effects"):client.access("scratch")
        finally:client.close()
        dsec.finish("later",True);dsec.reclaim("scratch")
        with self.assertRaisesRegex(RuntimeError,"session root"):dsec.reclaim("ledger")
        dsec.release_root("ledger");dsec.reclaim("ledger")

    def test_search_records_shared_backing_and_residency(self):
        self.store.access("ledger","write",text="alpha\nbeta\ngamma",expected_version=0)
        reply=self.store.access("ledger",query="beta")
        self.assertIn("2: beta",reply["text"])
        events=[json.loads(line) for p in (self.store.root/"events").glob("*.jsonl") for line in p.read_text().splitlines()]
        e=next(e for e in events if e.get("query")=="beta")
        self.assertEqual(e["backing_inode"],(self.payload/"ledger").stat().st_ino)
        self.assertEqual(e["nodes"],{"1":1})
        self.assertEqual(e["payload_capacity"],4096)


if __name__ == "__main__":
    unittest.main()
