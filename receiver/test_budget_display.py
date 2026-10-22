"""Offline budget upgrade/display regression. Temporary stores, no browser or API."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from server import App, rendered_page
from store import atomic, GateError

class NoNetworkWorker:
    def __init__(self,store):self.login={'state':'ready'};self.enqueued=[]
    def enqueue(self,tid):self.enqueued.append(tid)

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=App(self.root/'private',worker_factory=NoNetworkWorker)
        self.files=[]
        for i in range(3):
            p=self.root/f'{i}.wav';p.write_bytes(b'OFFLINE SYNTHETIC ONLY '+bytes([i]));self.files.append(p)
    def tearDown(self):self.tmp.cleanup()
    def complete(self,tid):
        t=self.app.store.load(tid);t['status']='complete';self.app.store.save(t)
    def old_submission(self):
        task,_=self.app.receive_audio(self.files[0]);self.app.store.reserve_submission(task['id']);self.complete(task['id'])
        return task,json.loads(self.app.store.budget.read_text())
    def test_preserve_one_allow_second_reject_third_and_reuse(self):
        old,budget=self.old_submission();original=copy.deepcopy(budget['submissions'])
        self.assertEqual((budget['limit'],len(original)),(None,1))           # r6 default: no cap
        budget['limit']=2;atomic(self.app.store.budget,budget)
        self.assertEqual(json.loads(self.app.store.budget.read_text())['submissions'],original)
        new,created=self.app.receive_audio(self.files[1]);self.assertTrue(created)
        self.app.store.reserve_submission(new['id']);self.complete(new['id'])
        after=json.loads(self.app.store.budget.read_text());self.assertEqual(after['submissions'][:1],original);self.assertEqual(len(after['submissions']),2)
        with self.assertRaisesRegex(GateError,'2/2'):self.app.receive_audio(self.files[2])
        for path,expected in [(self.files[0],old),(self.files[1],new)]:
            reused,created=self.app.receive_audio(path);self.assertFalse(created);self.assertEqual(reused['id'],expected['id'])
        self.assertEqual(json.loads(self.app.store.budget.read_text()),after)
        self.assertEqual(len(self.app.store.tasks()),2);self.assertEqual(len(self.app.worker.enqueued),2)
    def test_status_and_initial_html_use_real_cumulative_budget(self):
        _,budget=self.old_submission();budget['limit']=2;atomic(self.app.store.budget,budget)
        self.assertEqual((self.app.status()['used'],self.app.status()['limit']),(1,2))
        page=rendered_page(self.app,'existing').decode();self.assertIn('累计音频处理 1/2',page);self.assertNotIn('1/1',page)
        new,_=self.app.receive_audio(self.files[1]);self.app.store.reserve_submission(new['id']);self.complete(new['id'])
        self.assertEqual((self.app.status()['used'],self.app.status()['limit']),(2,2))
        self.assertIn('累计音频处理 2/2',rendered_page(self.app,'existing').decode())
    def test_no_limit_keeps_duplicate_protection(self):
        # 2026-10-20: limit null = no local count cap; dedup and the intent ledger remain.
        old,budget=self.old_submission();budget['limit']=None;atomic(self.app.store.budget,budget)
        extra=self.root/'3.wav';extra.write_bytes(b'OFFLINE SYNTHETIC ONLY '+bytes([3]));self.files.append(extra)
        for i in (1,2,3):
            t,created=self.app.receive_audio(self.files[i]);self.assertTrue(created)
            self.app.store.reserve_submission(t['id']);self.complete(t['id'])
        self.assertEqual(len(json.loads(self.app.store.budget.read_text())['submissions']),4)
        reused,created=self.app.receive_audio(self.files[0]);self.assertFalse(created);self.assertEqual(reused['id'],old['id'])
        with self.assertRaisesRegex(GateError,'禁止重复创建'):self.app.store.reserve_submission(old['id'])
        self.assertEqual((self.app.status()['used'],self.app.status()['limit']),(4,None))
        self.assertIn('累计音频处理 4 次（不限次数）',rendered_page(self.app,'existing').decode())
if __name__=='__main__':unittest.main()
