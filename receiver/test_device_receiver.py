"""LOCAL SIMULATOR ONLY: synthetic PCM, no provider/browser/hardware."""
import hashlib, http.client, json, socket, tempfile, threading, unittest, wave
from pathlib import Path
from device_receiver import DeviceReceiver, make_server, MAX_PCM, MAX_CHUNK, MAX_SESSION_SECONDS, GateError
SID='a'*32
PCM=b'\x12\x34'*160
class FakeStore:
    def __init__(self):self.tasks={}
    def load(self,tid):return self.tasks[tid]
class FakeApp:
    def __init__(self):self.calls=[];self.store=FakeStore()
    def receive_audio(self,path):
        self.calls.append(Path(path).read_bytes())
        self.store.tasks['test']={'status':'complete','share':{'status':'success','url':'https://example.invalid/LOCAL-SIMULATOR'}}
        return {'id':'test'},True
class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=FakeApp();self.now=100
        self.r=DeviceReceiver(self.tmp.name,self.app,clock=lambda:self.now)
    def tearDown(self):self.tmp.cleanup()
    def finish(self):return self.r.finish(SID,1,len(PCM),hashlib.sha256(PCM).hexdigest())
    def put(self):self.r.start(SID);return self.r.chunk(SID,0,PCM)
    def test_duplicate_start_single_active(self):
        self.put();self.assertEqual(self.r.start(SID)['next'],1)
        with self.assertRaises(GateError):self.r.start('b'*32)
    def test_duplicate_chunk(self):
        self.put();s=self.r.chunk(SID,0,PCM);self.assertEqual((s['bytes'],s['next']),(320,1))
        with self.assertRaises(GateError):self.r.chunk(SID,0,b'\0\0')
    def test_missing_chunk(self):
        self.r.start(SID)
        with self.assertRaises(GateError):self.r.chunk(SID,1,PCM)
        self.assertEqual(self.r.status(SID)['bytes'],0)
    def test_wrong_finish_count(self):
        self.put()
        with self.assertRaises(GateError):self.r.finish(SID,2,320,hashlib.sha256(PCM).hexdigest())
        self.assertEqual(self.r.status(SID)['state'],'error');self.assertFalse(self.app.calls)
    def test_wrong_hash(self):
        self.put()
        with self.assertRaises(GateError):self.r.finish(SID,1,320,'0'*64)
        self.assertFalse(self.app.calls)
    def test_wav_exact_no_processing(self):
        self.put();self.assertEqual(self.finish()['state'],'saved')
        with wave.open(str(Path(self.tmp.name)/SID/'original.wav')) as f:
            self.assertEqual((f.getnchannels(),f.getsampwidth(),f.getframerate()),(1,2,16000));self.assertEqual(f.readframes(160),PCM)
        self.assertFalse(self.app.calls)
    def test_repeat_finish(self):
        self.put();first=self.finish();self.assertEqual(first,self.finish())
        with self.assertRaises(GateError):self.r.finish(SID,1,320,'0'*64)
    def test_handoff_once_mock(self):
        self.r.allow_processing=True;self.put();s=self.finish();self.finish()
        self.assertEqual(len(self.app.calls),1);self.assertEqual(s['url'],'https://example.invalid/LOCAL-SIMULATOR')
    def test_no_old_url(self):
        self.app.store.tasks['old']={'status':'complete','share':{'status':'success','url':'https://example.invalid/old'}}
        self.assertNotIn('url',self.r.start(SID));self.put();self.assertNotIn('url',self.finish())
    def test_timeout_preserves(self):
        self.put();self.now+=11;self.assertEqual(self.r.status(SID)['state'],'error');self.assertTrue((Path(self.tmp.name)/SID/'000000.pcm').exists());self.assertFalse(self.app.calls)
    def test_restart_incomplete(self):
        self.put();r=DeviceReceiver(self.tmp.name,self.app);self.assertEqual(r.status(SID)['state'],'error');self.assertFalse(self.app.calls)
    def test_abort_idempotent(self):
        self.put();self.assertEqual(self.r.abort(SID),self.r.abort(SID));self.assertFalse(self.app.calls)
    def test_restart_saved(self):
        self.put();self.finish();r=DeviceReceiver(self.tmp.name,self.app);self.assertEqual(r.status(SID)['state'],'saved')
    def test_bad_pcm_and_session(self):
        with self.assertRaises(GateError):self.r.start('../private')
        self.r.start(SID)
        for data in (b'',b'x',b'x'*64002):
            with self.assertRaises(GateError):self.r.chunk(SID,0,data)
    def test_r2_mixed_small_and_batched_chunks(self):
        # meeting.14 sends 100 ms when caught up and up to 2 s while draining a backlog.
        big=b'\x01\x02'*(MAX_CHUNK//2);self.assertEqual(MAX_CHUNK,64000);self.r.start(SID)
        parts=[PCM*10,big,PCM,big[:32000]]
        for n,data in enumerate(parts):self.assertEqual(self.r.chunk(SID,n,data)['next'],n+1)
        self.assertEqual(self.r.chunk(SID,1,big)['bytes'],sum(map(len,parts)))  # retried batch: dedup, no append
        with self.assertRaises(GateError):self.r.chunk(SID,1,big[:-2]+b'\0\0')     # same seq, other bytes
        with self.assertRaises(GateError):self.r.chunk(SID,4,big+b'\0\0')          # over limit
        whole=b''.join(parts)
        self.assertEqual(self.r.finish(SID,4,len(whole),hashlib.sha256(whole).hexdigest())['state'],'saved');self.assertFalse(self.app.calls)
    def test_r2_session_wall_clock_cap(self):
        self.put()
        for n in range(1,41):self.now+=5;self.r.chunk(SID,n,PCM)              # steady chunks, 200 s in: r1 cap was 195 s
        self.assertEqual(self.r.status(SID)['state'],'recording')
        self.now=100+MAX_SESSION_SECONDS+1;self.assertEqual(self.r.status(SID)['state'],'error')
    def test_empty_finish(self):
        self.r.start(SID)
        with self.assertRaises(GateError):self.r.finish(SID,0,0,hashlib.sha256(b'').hexdigest())
    def test_ten_minute_audio_total_small_chunks(self):
        # Total-bytes limit only: the clock never moves, so wall-clock/inactivity cannot fire.
        self.assertEqual(MAX_PCM,19_200_000);data=PCM*10;self.r.start(SID)
        for n in range(1800):self.r.chunk(SID,n,data)
        self.assertEqual(self.r.status(SID)['state'],'recording')            # old 180 s point: no early stop
        for n in range(1800,6000):self.r.chunk(SID,n,data)
        self.assertEqual(self.r.status(SID)['bytes'],MAX_PCM)
        with self.assertRaises(GateError):self.r.chunk(SID,6000,b'\0\0')  # one sample over: rejected
        digest=hashlib.sha256(data*6000).hexdigest()
        first=self.r.finish(SID,6000,MAX_PCM,digest);self.assertEqual(first['state'],'saved')
        self.assertEqual(self.r.finish(SID,6000,MAX_PCM,digest),first)        # finish retry idempotent
        with wave.open(str(Path(self.tmp.name)/SID/'original.wav')) as f:self.assertEqual(f.getnframes(),MAX_PCM//2)
        self.assertFalse(self.app.calls)                                      # processing off: no handoff
    def test_ten_minute_audio_total_batched_chunks(self):
        big=bytes(range(256))*250;self.assertEqual(len(big),64000);self.r.start(SID)
        for n in range(300):self.r.chunk(SID,n,big)
        self.assertEqual(self.r.chunk(SID,299,big)['bytes'],MAX_PCM)          # retried batch: no append
        with self.assertRaises(GateError):self.r.chunk(SID,300,PCM)
        self.assertEqual(self.r.finish(SID,300,MAX_PCM,hashlib.sha256(big*300).hexdigest())['state'],'saved')
    def test_wall_clock_cap_660_independent_of_total(self):
        self.assertEqual(MAX_SESSION_SECONDS,660);self.put()
        for n in range(1,132):self.now+=5;self.r.chunk(SID,n,PCM)              # 655 s, chunks every 5 s, tiny total
        self.assertEqual(self.r.status(SID)['state'],'recording')
        self.now+=6;s=self.r.status(SID);self.assertEqual((s['state'],s['error']),('error','stream_timeout_incomplete'))
        self.assertLess(s['bytes'],MAX_PCM)
    def test_inactivity_10s_independent_of_wall_clock(self):
        self.put();self.now+=9.9;self.assertEqual(self.r.status(SID)['state'],'recording')
        self.r.chunk(SID,1,PCM);self.now+=10.1;self.assertEqual(self.r.status(SID)['state'],'error')
        self.assertLess(self.now-100,MAX_SESSION_SECONDS)
    def test_mock_handoff_unknown_not_retried(self):
        self.r.allow_processing=True
        def fail(path):self.app.calls.append(path);raise RuntimeError('unknown')
        self.app.receive_audio=fail;self.put();self.finish();self.finish();self.assertEqual(len(self.app.calls),1)
    def test_private_permissions(self):
        self.put();self.finish()
        for p in (Path(self.tmp.name)/SID).iterdir():self.assertEqual(p.stat().st_mode&0o077,0)
class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=FakeApp();self.r=DeviceReceiver(self.tmp.name,self.app)
        self.s=make_server(dict(bind='127.0.0.1',port=0,token='t'*32),self.r)
        self.t=threading.Thread(target=self.s.serve_forever);self.t.start()
    def tearDown(self):self.s.shutdown();self.s.server_close();self.t.join();self.tmp.cleanup()
    def request(self,path,body=b'',extra=None):
        c=http.client.HTTPConnection('127.0.0.1',self.s.server_port,timeout=5)
        headers={'Authorization':'Bearer '+'t'*32};headers.update(extra or {})
        c.request('POST',path,body,headers);r=c.getresponse();out=(r.status,json.loads(r.read()));c.close();return out
    def test_http_ack_retry_finish(self):
        self.assertEqual(self.request('/v1/start/'+SID)[0],200)
        a=self.request('/v1/chunk/'+SID+'/0',PCM);self.assertEqual(a,self.request('/v1/chunk/'+SID+'/0',PCM))
        out=self.request('/v1/finish/'+SID,json.dumps(dict(count=1,bytes=len(PCM),sha256=hashlib.sha256(PCM).hexdigest())).encode())
        self.assertEqual(out[1]['state'],'saved');self.assertFalse(self.app.calls)
    def test_security_boundary(self):
        for headers in ({'Authorization':''},{'Origin':'http://evil.invalid'},{'Origin':'null'},{'Host':'evil.invalid'}):
            self.assertEqual(self.request('/v1/start/'+SID,extra=headers)[0],403)
        for route in ('/api/stop','/private/browser-profile','/api/tasks','/api/status'):
            self.assertEqual(self.request(route)[0],409)
    def test_truncated_disconnect(self):
        self.request('/v1/start/'+SID);c=socket.create_connection(self.s.server_address)
        c.sendall(('POST /v1/chunk/'+SID+'/0 HTTP/1.1\r\nHost: '+self.s.expected_host+'\r\nAuthorization: Bearer '+'t'*32+'\r\nContent-Length: 320\r\n\r\n').encode()+b'x'*40)
        c.shutdown(socket.SHUT_WR)
        while c.recv(4096):pass
        c.close();self.assertEqual(self.r.status(SID)['bytes'],0);self.assertFalse(self.app.calls)

class ManagementBoundaryTests(unittest.TestCase):
    def test_existing_loopback_host_origin_csrf_guards(self):
        # Isolated HTTP handler test: no App, BrowserWorker, existing private store or UI automation.
        import server
        class UnusedApp:
            def status(self):return {'tasks':[]}
        s=server.Server(('127.0.0.1',0),server.Handler);s.app=UnusedApp()
        t=threading.Thread(target=s.serve_forever);t.start()
        def call(method,path,headers):
            c=http.client.HTTPConnection('127.0.0.1',s.server_port)
            c.request(method,path,b'',headers);r=c.getresponse();status=r.status;r.read();c.close();return status
        try:
            self.assertEqual(call('GET','/api/status',{'Host':'evil.invalid'}),403)
            self.assertEqual(call('GET','/private/browser-profile',{'Host':'127.0.0.1:8766'}),404)
            self.assertEqual(call('POST','/api/stop',{'Host':'127.0.0.1:8766','X-CSRF-Token':'wrong'}),403)
            for origin in ('null','http://evil.invalid'):
                self.assertEqual(call('POST','/api/stop',{'Host':'127.0.0.1:8766','Origin':origin,'X-CSRF-Token':server.TOKEN}),403)
        finally:s.shutdown();s.server_close();t.join()

if __name__=='__main__':unittest.main()
