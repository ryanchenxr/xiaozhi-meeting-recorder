import asyncio
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from store import Store, GateError, read
from browser import BrowserWorker
import official

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'private';self.store=Store(self.root);self.source=Path(self.tmp.name)/'sample.wav';self.source.write_bytes(b'local synthetic fixture')
    def tearDown(self):self.tmp.cleanup()
    def task(self):return self.store.receive_file(self.source)[0]
    def test_simultaneous_identical_files_have_one_identity(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.store.receive_file(self.source),range(8)))
        self.assertEqual(len({r[0]['id'] for r in results}),1);self.assertEqual(sum(r[1] for r in results),1)
    def test_single_active_task_rejects_other_file(self):
        self.task();p=self.source.with_name('other.wav');p.write_bytes(b'different')
        with self.assertRaises(GateError):self.store.receive_file(p)
    def test_submission_identity_is_durable_and_never_replayed(self):
        t=self.task();self.store.reserve_submission(t['id'])
        with self.assertRaises(GateError):self.store.reserve_submission(t['id'])
        resumed=Store(self.root);task=resumed.load(t['id']);self.assertTrue(task['submit_started']);self.assertEqual(task['status'],'unknown');self.assertEqual(len(read(resumed.budget,{})['submissions']),1)
    def test_restart_with_note_id_only_requires_read(self):
        t=self.task();self.store.reserve_submission(t['id']);t=self.store.load(t['id']);t.update(status='complete',note_id='1924400000000000001');self.store.save(t)
        r=Store(self.root).load(t['id']);self.assertEqual(r['note_id'],t['note_id']);self.assertTrue(r['restart_read_required']);self.assertTrue(r['submit_started'])
    def test_upload_unknown_cannot_become_local_retry_on_restart(self):
        t=self.task();t.update(status='running',remote_started=True,phase='put_intent',file_id='test-file');self.store.save(t)
        r=Store(self.root).load(t['id']);self.assertEqual(r['status'],'unknown');self.assertEqual(r['file_id'],'test-file')
    def test_only_local_interruption_remains_resumable(self):
        t=self.task();t.update(status='running',phase='converting');self.store.save(t)
        r=Store(self.root).load(t['id']);self.assertEqual(r['status'],'interrupted_local');self.assertFalse(r['submit_started'])
    def test_login_failure_retains_audio_and_identity(self):
        t=self.task();w=object.__new__(BrowserWorker);w.store=self.store;w.login={'state':'ready','tokenExpireAt':123};w._failure(t['id'],'needs_login');r=self.store.load(t['id']);self.assertEqual(r['status'],'needs_login');self.assertEqual(r['client_note_id'],t['client_note_id']);self.assertEqual((self.store.directory(t['id'])/r['original']).read_bytes(),self.source.read_bytes());self.assertEqual(w.login['state'],'needs_login')
    def test_raw_outputs_are_never_overwritten(self):
        t=self.task();self.assertTrue(self.store.text_once(t['id'],'summary.md','first raw'));self.assertFalse(self.store.text_once(t['id'],'summary.md','rewritten'));self.assertEqual((self.store.directory(t['id'])/'summary.md').read_text(),'first raw')
    def test_sharing_always_excludes_audio_and_uses_task_identity(self):
        with patch('official.request',return_value={'share_url':'https://biji.ddmaster.com/note/share_note/test123'}) as request:
            url=official.share('1924400000000000001','fixed-request');self.assertTrue(url.endswith('test123'));self.assertEqual(request.call_args.args[1],{'note_id':'1924400000000000001','share_exclude_audio':True});self.assertEqual(request.call_args.args[2],'fixed-request')
    def test_sharing_rejects_signed_or_foreign_url(self):
        for url in ['https://example.com/note/share_note/x','https://biji.ddmaster.com/note/share_note/x?token=secret']:
            with patch('official.request',return_value={'share_url':url}):
                with self.assertRaises(official.APIError):official.share('123','request')
    def test_private_permissions_and_task_path_guards(self):
        t=self.task();self.assertEqual(self.root.stat().st_mode&0o777,0o700);self.assertEqual((self.store.directory(t['id'])/'state.json').stat().st_mode&0o777,0o600)
        with self.assertRaises(GateError):self.store.load('../../config.local.json')
        public=self.store.public(t);self.assertNotIn('original',public);self.assertNotIn('browser-profile',json.dumps(public))

    def worker(self):
        w=object.__new__(BrowserWorker);w.store=self.store;w.login={'state':'ready','tokenExpireAt':123};return w
    def test_known_note_recovery_never_uses_browser_or_creates_sharing_again(self):
        t=self.task();t.update(note_id='1924400000000000001',submit_started=True,status='recovery_required');t['share'].update(status='success',url='https://biji.ddmaster.com/note/share_note/current');self.store.save(t)
        note={'content':'untouched raw summary','audio':{'sentences':[{'text':'raw sentence'}]}}
        with patch('official.detail',return_value=note) as detail,patch('official.share') as share:
            asyncio.run(self.worker()._process(t['id']));detail.assert_called_once_with(t['note_id']);share.assert_not_called()
        self.assertEqual(self.store.load(t['id'])['status'],'complete')
    def test_unknown_sharing_is_read_only_on_recovery(self):
        t=self.task();t.update(note_id='1924400000000000001',status='recovery_required');t['share']['status']='unknown';self.store.save(t)
        with patch('official.detail',return_value={'content':'summary','audio':{'sentences':[{'text':'raw'}]}}),patch('official.share') as share:
            asyncio.run(self.worker()._process(t['id']));share.assert_not_called()
        self.assertEqual(self.store.load(t['id'])['status'],'share_unknown')
    def test_unknown_submission_without_note_id_does_not_use_browser(self):
        t=self.task();t.update(submit_started=True,status='unknown');self.store.save(t)
        asyncio.run(self.worker()._process(t['id']));self.assertEqual(self.store.load(t['id'])['status'],'unknown')
    def test_logged_out_task_has_no_remote_submission(self):
        t=self.task();w=self.worker();w.login={'state':'needs_login'};w._login_status=AsyncMock()
        asyncio.run(w._process(t['id']));self.assertEqual(self.store.load(t['id'])['status'],'needs_login');self.assertEqual(len(read(self.store.budget,{})['submissions']),0)

    def test_repeated_note_identity_preserves_first_receipt_time(self):
        t=self.task();w=self.worker();w.pending={t['id']}
        asyncio.run(w._checkpoint(t['id'],'note_id',{'note_id':'1924400000000000001','ms':50}))
        asyncio.run(w._checkpoint(t['id'],'note_id',{'note_id':'1924400000000000001','ms':9000}))
        self.assertEqual(self.store.load(t['id'])['timings']['note_id_ms'],50)

if __name__=='__main__':unittest.main()
