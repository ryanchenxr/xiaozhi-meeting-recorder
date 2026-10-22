"""Temporary receiver only; no App, Chrome, provider, or production service."""
import hashlib,json,tempfile,threading,http.client,unittest
from pathlib import Path
from unittest.mock import patch
from device_receiver import DeviceReceiver,make_server,DIAGNOSTIC_FIELDS,validate_diagnostic
SID='a'*32
class NoProvider:
    def receive_audio(self,*args):raise AssertionError('No cloud allowed')
class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.r=DeviceReceiver(self.root,NoProvider(),False)
    def tearDown(self):self.tmp.cleanup()
    def payload(self,reason=5):return {'v':1,'session':SID,'first':{**dict.fromkeys(DIAGNOSTIC_FIELDS,0),'reason':reason,'time_us':100,'seq':3,'state':2,'attempt':2,'http':200,'ack':2,'in_flight':False,'ack_session_match':True,'Authorization':'MUST_NOT_SAVE'},'token':'MUST_NOT_SAVE'}
    def test_old_abort_and_new_diagnostic_do_not_change_state(self):
        self.r.start(SID);old=self.r.abort(SID)
        self.assertEqual(old['error'],'device_aborted_incomplete')
        self.assertEqual(self.r.abort(SID,self.payload()),old)
        saved=(self.root/SID/'device-first-error.json').read_text();self.assertNotIn('MUST_NOT_SAVE',saved)
        self.r.abort(SID,self.payload(6));self.assertEqual((self.root/SID/'device-first-error.json').read_text(),saved)
    def test_every_missing_context_field_rejected_without_changing_abort(self):
        self.r.start(SID)
        for key in DIAGNOSTIC_FIELDS|{'in_flight','ack_session_match'}:
            value=self.payload();del value['first'][key]
            self.assertIsNone(validate_diagnostic(SID,value))
            self.assertEqual(self.r.abort(SID,value)['state'],'error')
        self.assertFalse((self.root/SID/'device-first-error.json').exists())
        self.assertEqual(self.r.abort(SID,self.payload())['state'],'error')
        self.assertTrue((self.root/SID/'device-first-error.json').exists())

    def test_bad_payload_never_changes_abort_decision(self):
        self.r.start(SID)
        for bad in [False,{},dict(self.payload(),session='b'*32),{'v':1,'session':SID,'first':{'reason':5,'time_us':'secret'}},dict(self.payload(),first={'reason':999,'time_us':100})]:
            self.assertEqual(self.r.abort(SID,bad)['state'],'error')
        self.assertFalse((self.root/SID/'device-first-error.json').exists())
    def test_event_bound_and_no_frame_logs(self):
        self.r.start(SID)
        for i in range(5):self.r.chunk(SID,i,b'\0\0')
        events=json.loads((self.root/SID/'receiver-events.json').read_text());self.assertEqual(len(events['events']),1)
        for _ in range(80):self.r.start(SID)
        events=json.loads((self.root/SID/'receiver-events.json').read_text());self.assertEqual(len(events['events']),32);self.assertEqual(events['dropped'],49)
    def test_diagnostic_write_failure_does_not_break_finish_or_abort(self):
        from store import atomic as real_atomic
        def fail_diagnostic(path,data):
            if Path(path).name in ('receiver-events.json','device-first-error.json'):raise OSError('simulated diagnostic disk failure')
            return real_atomic(path,data)
        with patch('device_receiver.atomic',side_effect=fail_diagnostic):
            self.r.start(SID);self.r.chunk(SID,0,b'\0\0');self.assertEqual(self.r.abort(SID,self.payload())['state'],'error')
            other='b'*32;self.r.start(other);self.r.chunk(other,0,b'\0\0')
            self.assertEqual(self.r.finish(other,1,2,hashlib.sha256(b'\0\0').hexdigest())['state'],'saved')
    def test_recovery_and_finish_events_are_separate_from_business_state(self):
        self.r.start(SID);r=DeviceReceiver(self.root,NoProvider(),False)
        self.assertEqual(r.status(SID)['error'],'receiver_restarted_incomplete_or_unknown')
        events=json.loads((self.root/SID/'receiver-events.json').read_text())['events'];self.assertEqual([e['kind'] for e in events],['start','recovery'])
        other='b'*32;r.start(other);r.chunk(other,0,b'\0\0');r.finish(other,1,2,hashlib.sha256(b'\0\0').hexdigest())
        events=json.loads((self.root/other/'receiver-events.json').read_text())['events'];self.assertEqual([e['kind'] for e in events],['start','finish_requested','finish_result'])
    def test_http_old_empty_and_new_body_and_auth(self):
        server=make_server({'bind':'127.0.0.1','port':0,'token':'t'*32},self.r);t=threading.Thread(target=server.serve_forever);t.start()
        def call(path,body=b'',auth=True):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port)
            c.request('POST',path,body,{'Authorization':'Bearer '+'t'*32 if auth else 'wrong'});res=c.getresponse();data=json.loads(res.read());c.close();return res.status,data
        try:
            self.assertEqual(call('/v1/start/'+SID)[0],200)
            self.assertEqual(call('/v1/abort/'+SID)[1]['state'],'error')
            self.assertEqual(call('/v1/abort/'+SID,json.dumps(self.payload()).encode())[0],200)
            self.assertEqual(call('/v1/abort/'+SID,b'bad json')[0],200)
            self.assertEqual(call('/v1/abort/'+SID,json.dumps(self.payload()).encode(),False)[0],403)
            self.assertEqual(call('/v1/chunk/'+SID+'/99',b'\0\0')[0],409)
            events=json.loads((self.root/SID/'receiver-events.json').read_text())['events'];self.assertTrue(any(e['kind']=='rejected' for e in events))
        finally:server.shutdown();server.server_close();t.join()
if __name__=='__main__':unittest.main()
