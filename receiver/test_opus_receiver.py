"""LOCAL SIMULATOR ONLY: synthetic Opus via local libopus, no device/provider/browser."""
import hashlib, http.client, json, math, struct, subprocess, tempfile, threading, time, unittest, wave
from pathlib import Path
import opus_codec as oc
import device_receiver, store
from device_receiver import (DeviceReceiver, make_server, GateError, validate_diagnostic,
    OPUS_MAX_SAMPLES, OPUS_MAX_BYTES, OPUS_INACTIVITY_SECONDS, OPUS_MAX_SESSION_SECONDS, MAX_CHUNK, MAX_SEQ,
    HANDOFF_WAV_MAX_BYTES, FFPROBE)
SID='b'*32
def tone(seconds, f=440):
    return b''.join(struct.pack('<h', int(6000*math.sin(2*math.pi*f*i/16000))) for i in range(int(16000*seconds)))
PACKETS=oc.encode(tone(3.0))                     # 50 packets
class App:
    def __init__(self): self.calls=[]; self.store=None
    def receive_audio(self, p): self.calls.append(p); raise AssertionError('processing must stay off')
class OpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.now=100; self.app=App()
        self.r=DeviceReceiver(self.tmp.name, self.app, clock=lambda:self.now)
    def tearDown(self): self.tmp.cleanup()
    def chunks(self, sizes):
        out, i = [], 0
        for n in sizes: out.append(oc.frame(PACKETS[i:i+n])); i += n
        return out
    def test_full_flow_padding_dedup_and_idempotent_finish(self):
        self.assertEqual(self.r.start(SID,'opus')['state'],'recording')
        parts=self.chunks([8,30,12])
        for n,c in enumerate(parts): self.assertEqual(self.r.chunk(SID,n,c,codec='opus')['next'],n+1)
        self.assertEqual(self.r.chunk(SID,1,parts[1],codec='opus')['bytes'],sum(map(len,parts)))   # retry: no append
        with self.assertRaises(GateError): self.r.chunk(SID,1,parts[2],codec='opus')              # same seq, other bytes
        stream=b''.join(parts); samples=50*960-123                                                # last packet padded
        ident=(3,len(stream),hashlib.sha256(stream).hexdigest(),50,samples)
        self.assertEqual(self.r.finish_opus(SID,*ident)['state'],'processing')                  # verified; decode async
        first=self.r.join_finish(SID); self.assertEqual(first['state'],'saved')
        self.assertEqual(self.r.finish_opus(SID,*ident),first)                                   # idempotent
        with self.assertRaises(GateError): self.r.finish_opus(SID,3,len(stream),'0'*64,50,samples)
        with wave.open(str(Path(self.tmp.name)/SID/'original.wav')) as w:
            self.assertEqual((w.getframerate(),w.getnchannels(),w.getsampwidth(),w.getnframes()),(16000,1,2,samples))
            self.assertEqual(w.readframes(samples), oc.decode(PACKETS)[:samples*2])
        self.assertFalse(self.app.calls)
    def test_malformed_and_codec_mismatch_rejected_without_state_change(self):
        self.r.start(SID,'opus')
        for bad in (b'', b'\x00\x00', b'\x05\x00ab', b'\x01', oc.frame(PACKETS)[:-1], b'\x00'*(MAX_CHUNK+2)):
            with self.assertRaises(GateError): self.r.chunk(SID,0,bad,codec='opus')
        with self.assertRaises(GateError): self.r.chunk(SID,0,b'\0\0'*160)                         # v1 PCM to opus session
        with self.assertRaises(GateError): self.r.start(SID)                                       # restart as PCM
        self.assertEqual(self.r.status(SID)['bytes'],0)
    def test_finish_mismatches_mark_error_and_never_hand_off(self):
        for wrong in ('packets','samples_high','samples_low','count'):
            with self.subTest(wrong=wrong):
                sid=hashlib.md5(wrong.encode()).hexdigest()
                for old in Path(self.tmp.name).glob('*/state.json'):                              # one active session
                    st=json.loads(old.read_text()); st['state']='error'; old.write_text(json.dumps(st))
                self.r.start(sid,'opus'); c=self.chunks([50])[0]; self.r.chunk(sid,0,c,codec='opus')
                args=dict(count=1,size=len(c),digest=hashlib.sha256(c).hexdigest(),packets=50,samples=50*960)
                if wrong=='packets':args['packets']=49
                if wrong=='samples_high':args['samples']=50*960+1
                if wrong=='samples_low':args['samples']=49*960
                if wrong=='count':args['count']=2
                with self.assertRaises(GateError): self.r.finish_opus(sid,**args)
                self.assertEqual(self.r.status(sid)['state'],'error')
                self.assertFalse((Path(self.tmp.name)/sid/'original.wav').exists())
        self.assertFalse(self.app.calls)
    def test_sample_limit_3600s(self):
        self.r.start(SID,'opus'); one=oc.frame(PACKETS[:1]); per=MAX_CHUNK//len(one)
        sent,seq=0,0
        while sent<OPUS_MAX_SAMPLES//960:
            n=min(per,OPUS_MAX_SAMPLES//960-sent); self.r.chunk(SID,seq,one*n,codec='opus'); sent+=n; seq+=1
        self.assertEqual((OPUS_MAX_SAMPLES,self.r.load(SID)['packets']),(57600000,60000))
        with self.assertRaises(GateError): self.r.chunk(SID,seq,one,codec='opus')
    def test_inactivity_630s_and_wall_5400s(self):
        self.assertEqual((OPUS_INACTIVITY_SECONDS,OPUS_MAX_SESSION_SECONDS),(630,5400))
        self.r.start(SID,'opus'); c=self.chunks([1]*50)
        self.now+=629.9; self.r.chunk(SID,0,c[0],codec='opus'); self.assertEqual(self.r.status(SID)['state'],'recording')  # 10.5 min silence survives
        for n in range(1,8): self.now+=620; self.r.chunk(SID,n,c[n],codec='opus')                  # 4969.9 s
        self.now+=420; self.r.chunk(SID,8,c[8],codec='opus')                                       # 5389.9 s
        self.assertEqual(self.r.status(SID)['state'],'recording')
        self.now+=11; self.assertEqual(self.r.status(SID)['state'],'error')                         # > 5400 s wall
        sid='c'*32; self.r.start(sid,'opus'); self.now+=630.1; self.assertEqual(self.r.status(sid)['state'],'error')
    def test_new_diagnostic_reasons(self):
        base={k:0 for k in ('state','seq','sampled','enqueued','dequeued','tx_pcm_bytes','acked_bytes','acked_chunks','queue_frames','capture_code','stop_source','stop_us','stop_requests','phase','attempt','elapsed_us','error_kind','code','http','ack','expected_next','actual_next','request_written','time_us')}
        for reason,ok in ((17,True),(18,True),(19,False)):
            v={'v':1,'session':SID,'first':dict(base,reason=reason,in_flight=False,ack_session_match=False)}
            self.assertEqual(validate_diagnostic(SID,v) is not None, ok)
class OpusHttpTests(unittest.TestCase):
    def test_v2_over_real_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=DeviceReceiver(tmp,App()); srv=make_server({'bind':'127.0.0.1','port':0,'token':'t'*40},r)
            t=threading.Thread(target=srv.serve_forever,kwargs={'poll_interval':.01}); t.start()
            c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=5)
            def post(path,body=b''):
                c.request('POST',path,body,{'Host':srv.expected_host,'Authorization':'Bearer '+'t'*40})
                resp=c.getresponse(); return resp.status,json.loads(resp.read())
            try:
                self.assertEqual(post(f'/v2/start/{SID}')[1]['state'],'recording')
                a,b=oc.frame(PACKETS[:20]),oc.frame(PACKETS[20:])
                self.assertEqual(post(f'/v2/chunk/{SID}/0',a)[1]['next'],1)
                self.assertEqual(post(f'/v2/chunk/{SID}/1',b)[1]['next'],2)
                self.assertEqual(post(f'/v1/chunk/{SID}/2',b'\0\0')[0],409)                        # wrong codec route
                body=json.dumps({'count':2,'bytes':len(a+b),'sha256':hashlib.sha256(a+b).hexdigest(),'packets':50,'samples':48000}).encode()
                st,v=post(f'/v2/finish/{SID}',body); self.assertEqual((st,v['state']),(200,'processing'))
                r.join_finish(SID); self.assertEqual(post(f'/v2/status/{SID}')[1]['state'],'saved')
                # Sequence bound is MAX_SEQ=60,000 (one packet per chunk at most 60,000 chunks).
                sid2='d'*32; post(f'/v2/start/{sid2}')
                self.assertEqual(post(f'/v2/chunk/{sid2}/{MAX_SEQ-1}',a)[0],409)                  # in range, but a gap
                self.assertEqual(post(f'/v2/chunk/{sid2}/{MAX_SEQ}',a)[0],409)
                reasons=[e.get('reason') for e in json.loads((Path(tmp)/sid2/'receiver-events.json').read_text())['events'] if e['kind']=='rejected']
                self.assertEqual(reasons,['missing_chunk','invalid_sequence'])
            finally:
                c.close(); srv.shutdown(); srv.server_close(); t.join()

TEN_S=oc.encode(b''.join(struct.pack('<h', int(5000*math.sin(2*math.pi*(300+40*math.sin(i/4000))*i/16000)))
    for i in range(16000*10)), bitrate=32000)                           # 10 s of real 32 kbit/s VBR packets
class StoreApp:
    """Real store admission (EXTENSIONS, 64 MiB) without the browser worker."""
    def __init__(self, root): self.store=store.Store(root); self.calls=[]
    def receive_audio(self, p, name=None):
        self.calls.append(Path(p)); task,_=self.store.receive_file(p, name); return self.store.public(task), True
class SixtyMinuteTests(unittest.TestCase):
    """No device, no Dedao: real libopus packets, real receiver, real ffmpeg, real store admission."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.app=StoreApp(Path(self.tmp.name)/'store')
        self.r=DeviceReceiver(Path(self.tmp.name)/'device', self.app, allow_processing=True)
    def tearDown(self): self.tmp.cleanup()
    def stream(self, sid, packets, per=8):
        self.r.start(sid,'opus'); h=hashlib.sha256(); size=count=0
        for i in range(0,packets,per):
            c=oc.frame([TEN_S[(i+k)%len(TEN_S)] for k in range(min(per,packets-i))])
            self.assertEqual(self.r.chunk(sid,count,c,codec='opus')['next'],count+1); h.update(c); size+=len(c); count+=1
        return count,size,h.hexdigest()
    def test_sixty_minutes_decode_exact_and_hand_off_m4a(self):
        packets=OPUS_MAX_SAMPLES//960; count,size,digest=self.stream(SID,packets)
        self.assertEqual((packets,count),(60000,7500)); self.assertLess(size,OPUS_MAX_BYTES//2)
        t=time.monotonic(); v=self.r.finish_opus(SID,count,size,digest,packets,OPUS_MAX_SAMPLES); finish_s=time.monotonic()-t
        self.assertEqual(v['state'],'processing'); self.assertLess(finish_s,1.2)                # device HTTP timeout
        self.assertEqual(self.r.status(SID)['state'],'processing')
        self.r.join_finish(SID); s=self.r.load(SID); d=Path(self.tmp.name)/'device'/SID
        with wave.open(str(d/'original.wav')) as w: self.assertEqual((w.getnframes(),w.getframerate(),w.getnchannels()),(57600000,16000,1))
        self.assertGreater((d/'original.wav').stat().st_size,store.MAX_BYTES)                   # why the WAV is not handed over
        self.assertEqual((s['state'],s['handoff_file']),('processing','original.m4a'))
        self.assertEqual([p.name for p in self.app.calls],['original.m4a'])
        task=self.app.store.load(s['task_id']); m4a=self.app.store.directory(task['id'])/task['original']
        self.assertEqual((task['original'],task['status']),('original.m4a','received'))
        self.assertLessEqual(m4a.stat().st_size,store.MAX_BYTES)
        probe=json.loads(subprocess.run([FFPROBE,'-v','error','-show_entries','format=duration','-of','json',str(m4a)],capture_output=True,check=True).stdout)
        self.assertAlmostEqual(float(probe['format']['duration']),3600.0,delta=0.25)
        print(f"\n60 min: {count} chunks, {size} B Opus, finish {finish_s*1000:.0f} ms, WAV {(d/'original.wav').stat().st_size} B, m4a {m4a.stat().st_size} B")
        with self.assertRaises(GateError): self.r.chunk(SID,count,oc.frame(TEN_S[:1]),codec='opus')
    def test_short_session_still_hands_off_wav(self):
        count,size,digest=self.stream(SID,500)                                                 # 30 s
        self.r.finish_opus(SID,count,size,digest,500,500*960-17); self.r.join_finish(SID)
        self.assertEqual([p.name for p in self.app.calls],['original.wav'])
        self.assertFalse((Path(self.tmp.name)/'device'/SID/'original.m4a').exists())
    def test_compression_failure_is_explicit_error_and_never_hands_off(self):
        old=(device_receiver.HANDOFF_WAV_MAX_BYTES,device_receiver.FFMPEG)
        device_receiver.HANDOFF_WAV_MAX_BYTES,device_receiver.FFMPEG=0,'/nonexistent/ffmpeg'
        try:
            count,size,digest=self.stream(SID,100); self.r.finish_opus(SID,count,size,digest,100,96000)
            v=self.r.join_finish(SID)
        finally: device_receiver.HANDOFF_WAV_MAX_BYTES,device_receiver.FFMPEG=old
        self.assertEqual((v['state'],v['error']),('error','local_decode_failed')); self.assertFalse(self.app.calls)
        self.assertTrue((Path(self.tmp.name)/'device'/SID/'original.wav').exists())             # archive kept
    def test_forced_m4a_on_short_session_and_restart_during_handoff(self):
        old=device_receiver.HANDOFF_WAV_MAX_BYTES; device_receiver.HANDOFF_WAV_MAX_BYTES=0
        try:
            count,size,digest=self.stream(SID,100); self.r.finish_opus(SID,count,size,digest,100,96000-5); self.r.join_finish(SID)
        finally: device_receiver.HANDOFF_WAV_MAX_BYTES=old
        self.assertEqual([p.name for p in self.app.calls],['original.m4a'])
        p=Path(self.tmp.name)/'device'/('e'*32); p.mkdir()
        (p/'state.json').write_text(json.dumps(dict(session='e'*32,state='handoff',next=1,bytes=1,started=0,last=0,codec='opus',packets=1)))
        r=DeviceReceiver(Path(self.tmp.name)/'device',self.app)
        self.assertEqual(r.status('e'*32)['error'],'receiver_restarted_incomplete_or_unknown')
if __name__=='__main__': unittest.main()
