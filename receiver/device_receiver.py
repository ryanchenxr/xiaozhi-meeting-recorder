"""Bounded LAN PCM protocol. No management/files/browser routes on this listener."""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import wave
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from store import atomic, GateError, MAX_BYTES as STORE_MAX_BYTES
from request_timing import RequestTimings
import opus_codec

# 10-minute local verification: firmware MeetingPcm::MaxSeconds=600.
MAX_PCM = 16000 * 2 * 600
# Device batches queued 10 ms frames: 100 ms when caught up, up to 2 s (200
# frames) while draining a backlog. Must equal the firmware MaxChunkFrames*320.
MAX_CHUNK = 64000
# Session wall-clock cap: 600 s audio + firmware 15 s queue drain + 8 s per-chunk
# retry window + margin. Inactivity stays 10 s (> firmware 8 s retry window).
MAX_SESSION_SECONDS = 660
# meeting.16 /v2: device-encoded 16 kHz mono Opus, 60 ms packets, framed as
# [u16 LE length][packet]. Same chunk size/dedup/ACK rules; limits by samples.
# meeting.18: firmware MeetingPcm::MaxSeconds=3600 (60-minute meetings).
OPUS_MAX_SAMPLES = 16000 * 3600            # 57,600,000 samples = 60,000 packets
OPUS_MAX_BYTES = 32 * 1024 * 1024          # ~2.3x the 14.5 MB framed stream at 32 kbit/s for 3600 s
# r5 / meeting.20: 3 MiB device ring (~13 min) and a 600 s per-chunk retry window,
# after two 60 min runs died when the home uplink nearly stalled for 5-6 minutes.
OPUS_INACTIVITY_SECONDS = 630              # > firmware 600 s per-chunk retry window
OPUS_MAX_SESSION_SECONDS = 5400            # 3600 s + a ~13 min stall (ring) + backlog drain + margin
# Every chunk carries >=1 packet, so 60,000 packets bound the chunk count (live
# meeting.17: ~0.48 s per chunk, ~7,500 for 60 min). Route accepts up to 5 digits.
MAX_SEQ = 60000
# Finish verifies identity and hash only, then answers "processing": decoding 60 min
# takes >1.2 s (the device's HTTP timeout). Decode, WAV and compression run on a
# worker thread outside the lock; status reports "processing" until handoff ends.
# Dedao admission is 64 MiB (store.MAX_BYTES); a 60 min WAV is ~115 MB. A WAV up to
# HANDOFF_WAV_MAX_BYTES (~17 min) is handed over as before; a longer one as an
# AAC-LC .m4a (store.EXTENSIONS, ~22 MB/hour). original.wav is always kept.
HANDOFF_WAV_MAX_BYTES = 32 * 1024 * 1024
HANDOFF_AAC_BITRATE = '48k'
FFMPEG = os.environ.get('MEETING_FFMPEG', '/opt/homebrew/bin/ffmpeg')
FFPROBE = os.environ.get('MEETING_FFPROBE', '/opt/homebrew/bin/ffprobe')
ID = re.compile(r'^[0-9a-f]{32}$')

# Only fixed numeric diagnostics are retained; unknown fields/raw headers are discarded.
DIAGNOSTIC_FIELDS={'state','seq','sampled','enqueued','dequeued','tx_pcm_bytes','acked_bytes',
    'acked_chunks','queue_frames','capture_code','stop_source','stop_us','stop_requests','phase',
    'attempt','elapsed_us','error_kind','code','http','ack','expected_next','actual_next',
    'request_written','reason','time_us'}
def validate_diagnostic(sid, value):
    if not isinstance(value,dict) or value.get('v')!=1 or value.get('session')!=sid:return None
    first=value.get('first')
    if not isinstance(first,dict) or type(first.get('reason')) is not int or not 1<=first['reason']<=18:return None
    if not DIAGNOSTIC_FIELDS.issubset(first):return None
    if not {'in_flight','ack_session_match'}.issubset(first):return None
    if 'first_present' in value and value['first_present'] is not True:return None
    clean={}
    for key in DIAGNOSTIC_FIELDS:
        if key in first:
            n=first[key]
            if type(n) is not int or not -(2**31)<=n<=2**53:return None
            clean[key]=n
    for key in ('in_flight','ack_session_match'):
        if key in first:
            if type(first[key]) is not bool:return None
            clean[key]=first[key]
    if 'time_us' not in clean:return None
    return {'v':1,'session':sid,'first':clean}

class DeviceReceiver:
    def __init__(self, root, app, allow_processing=False, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.app, self.allow_processing, self.clock = app, allow_processing, clock
        self.lock = threading.RLock()
        self.timings = RequestTimings(self.root)
        self.workers = {}
        for p in self.root.glob('*/state.json'):
            s = json.loads(p.read_text())
            if s['state'] in ('recording', 'handoff'):
                s.update(state='error', error='receiver_restarted_incomplete_or_unknown')
                atomic(p, s)
                self.event(s['session'],'recovery',state=s['state'],reason=s['error'])

    def event(self, sid, kind, **fields):
        """Best-effort bounded metadata. Never affects admission or integrity decisions."""
        try:
            with self.lock:
                directory=self.directory(sid)
                if not (directory/'state.json').exists(): return
                p=directory/'receiver-events.json'
                record=json.loads(p.read_text()) if p.exists() else {'v':1,'events':[],'dropped':0}
                if len(record['events'])<32:
                    record['events'].append({'at':self.clock(),'kind':kind,**fields})
                else: record['dropped']=min(record['dropped']+1,2147483647)
                atomic(p,record)
        except Exception:
            pass  # Diagnostics must not turn a valid request into a recording failure.

    def save_diagnostic(self, sid, diagnostic):
        if diagnostic is None: return
        try:
            cleaned=validate_diagnostic(sid,diagnostic)
            if cleaned is None:
                self.event(sid,'diagnostic_rejected');return
            target=self.directory(sid)/'device-first-error.json'
            if not target.exists(): atomic(target,{'received_at':self.clock(),'diagnostic':cleaned})
        except Exception:
            self.event(sid,'diagnostic_write_failed')

    def directory(self, sid):
        if not ID.fullmatch(sid): raise GateError('invalid_session')
        return self.root / sid

    def load(self, sid):
        try: return json.loads((self.directory(sid)/'state.json').read_text())
        except FileNotFoundError: raise GateError('unknown_session')

    def save(self, s): atomic(self.directory(s['session'])/'state.json', s)

    def expire(self, s):
        opus = s.get('codec') == 'opus'
        idle, wall = (OPUS_INACTIVITY_SECONDS, OPUS_MAX_SESSION_SECONDS) if opus else (10, MAX_SESSION_SECONDS)
        if s['state'] == 'recording' and (self.clock()-s['last'] > idle or self.clock()-s['started'] > wall):
            s.update(state='error', error='stream_timeout_incomplete'); self.save(s)
            self.event(s['session'],'timeout',next=s['next'],bytes=s['bytes'])
        return s

    def status(self, sid):
        with self.lock:
            s = self.expire(self.load(sid))
            result = {k:s[k] for k in ('session','state','next','bytes')}
            if 'error' in s: result['error']=s['error']
            if s['state']=='handoff': result['state']='processing'   # finish accepted; decode/handoff running
            if s.get('task_id'):
                t = self.app.store.load(s['task_id'])
                result['state'] = 'complete' if t['status']=='complete' and t['share']['status']=='success' else 'processing'
                if t['status'] in ('unknown','share_unknown','rejected','needs_login','login_required','error','failed','local_failed','configuration_required','interrupted_local','recovery_required'):
                    result.update(state='error', error=t['status'])
                if result['state']=='complete': result['url']=t['share']['url']
            return result

    def start(self, sid, codec='pcm'):
        with self.lock:
            p=self.directory(sid)
            if (p/'state.json').exists():
                if self.load(sid).get('codec','pcm')!=codec: raise GateError('codec_mismatch')
                self.event(sid,'start_reused');return self.status(sid)
            for old in self.root.glob('*/state.json'):
                s=self.expire(json.loads(old.read_text()))
                if s['state'] in ('recording','handoff') or (s.get('task_id') and self.status(s['session'])['state']=='processing'):
                    raise GateError('another_session_active')
            p.mkdir(mode=0o700)
            s=dict(session=sid,state='recording',next=0,bytes=0,started=self.clock(),last=self.clock())
            if codec=='opus': s.update(codec='opus',packets=0)
            self.save(s); self.event(sid,'start',state=s['state']);return self.status(sid)

    def chunk(self, sid, seq, data, timing=None, codec='pcm'):
        with self.lock:
            s=self.expire(self.load(sid))
            if s.get('codec','pcm')!=codec: raise GateError('codec_mismatch')
            if codec=='opus': return self._chunk_opus(s, sid, seq, data, timing)
            p=self.directory(sid)/f'{seq:06d}.pcm'
            if s['state']!='recording': raise GateError('not_recording')
            if not 0<len(data)<=MAX_CHUNK or len(data)%2: raise GateError('invalid_pcm')
            if seq<s['next']:
                self.timings.field(timing,'duplicate',True)
                if p.read_bytes()!=data: raise GateError('conflicting_duplicate')
                return self.status(sid)
            if seq!=s['next']: raise GateError('missing_chunk')
            if s['bytes']+len(data)>MAX_PCM: raise GateError('duration_limit')
            # Chunk is durable BEFORE ack/state; a crash never silently completes it.
            started=self.timings.now()
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:
                try:f.write(data);f.flush()
                finally:self.timings.elapsed(timing,'pcm_write_us',started)
                started=self.timings.now()
                try:os.fsync(f.fileno())
                finally:self.timings.elapsed(timing,'pcm_fsync_us',started)
            s.update(next=seq+1,bytes=s['bytes']+len(data),last=self.clock())
            started=self.timings.now()
            try:self.save(s)
            finally:self.timings.elapsed(timing,'state_save_us',started)
            return self.status(sid)

    def _persist(self, s, p, data, timing, **update):
        # Chunk is durable BEFORE ack/state; a crash never silently completes it.
        started=self.timings.now()
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:
            try:f.write(data);f.flush()
            finally:self.timings.elapsed(timing,'pcm_write_us',started)
            started=self.timings.now()
            try:os.fsync(f.fileno())
            finally:self.timings.elapsed(timing,'pcm_fsync_us',started)
        s.update(last=self.clock(),**update)
        started=self.timings.now()
        try:self.save(s)
        finally:self.timings.elapsed(timing,'state_save_us',started)

    def _chunk_opus(self, s, sid, seq, data, timing):
        p=self.directory(sid)/f'{seq:06d}.opus'
        if s['state']!='recording': raise GateError('not_recording')
        if not 0<len(data)<=MAX_CHUNK: raise GateError('invalid_opus')
        try: packets=len(opus_codec.split_frames(data))
        except ValueError: raise GateError('invalid_opus')
        if seq<s['next']:
            self.timings.field(timing,'duplicate',True)
            if p.read_bytes()!=data: raise GateError('conflicting_duplicate')
            return self.status(sid)
        if seq!=s['next']: raise GateError('missing_chunk')
        if (s['packets']+packets)*opus_codec.FRAME>OPUS_MAX_SAMPLES or s['bytes']+len(data)>OPUS_MAX_BYTES: raise GateError('duration_limit')
        self._persist(s,p,data,timing,next=seq+1,bytes=s['bytes']+len(data),packets=s['packets']+packets)
        return self.status(sid)

    def finish_opus(self, sid, count, size, digest, packets, samples):
        with self.lock:
            s=self.expire(self.load(sid))
            if s.get('codec')!='opus': raise GateError('codec_mismatch')
            self.event(sid,'finish_requested',count=count if type(count) is int and 0<=count<=MAX_SEQ else -1,bytes=size if type(size) is int and 0<=size<=OPUS_MAX_BYTES else -1)
            identity=[count,size,digest,packets,samples]
            if s.get('finish'):
                if s['finish']!=identity: raise GateError('conflicting_finish')
                self.event(sid,'finish_reused');return self.status(sid)
            if s['state']!='recording': raise GateError('not_recording')
            files=[self.directory(sid)/f'{n:06d}.opus' for n in range(s['next'])]
            h=hashlib.sha256();stream=bytearray()
            for f in files:
                b=f.read_bytes();h.update(b);stream+=b
            ok=(type(packets) is int and type(samples) is int and count==s['next'] and size==s['bytes'] and size>0
                and digest==h.hexdigest() and packets==s['packets'] and (packets-1)*opus_codec.FRAME<samples<=packets*opus_codec.FRAME)
            if ok:
                try: ok=len(opus_codec.split_frames(bytes(stream)))==packets
                except ValueError: ok=False
            if not ok:
                s.update(state='error',error='incomplete_hash_or_decode_mismatch');self.save(s)
                self.event(sid,'finish_rejected',reason=s['error'])
                raise GateError('incomplete_or_hash_mismatch')
            s.update(finish=identity,state='handoff');self.save(s)
            worker=threading.Thread(target=self._finish_opus_worker,args=(sid,bytes(stream),packets,samples),daemon=True)
            self.workers[sid]=worker;worker.start()
            return self.status(sid)

    def join_finish(self, sid, timeout=None):
        """Tests/shutdown: wait for the decode/handoff worker of one session."""
        worker=self.workers.get(sid)
        if worker: worker.join(timeout)
        return self.status(sid)

    def _finish_opus_worker(self, sid, stream, packets, samples):
        # Chunk files are immutable once state left 'recording'; no lock while decoding.
        directory=self.directory(sid)
        try:
            pcm=opus_codec.decode(opus_codec.split_frames(stream))
            if len(pcm)!=packets*opus_codec.FRAME*2: raise ValueError('decode_length')
            pcm=pcm[:samples*2]                      # drop the zero padding of the last packet
            target=directory/'original.wav'
            temp=directory/'original.wav.tmp'
            with wave.open(str(temp),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframesraw(pcm)
            os.chmod(temp,0o600)
            with temp.open('rb') as f:os.fsync(f.fileno())
            os.replace(temp,target)
            decoded=hashlib.sha256(pcm).hexdigest();del pcm
            handoff=target if target.stat().st_size<=HANDOFF_WAV_MAX_BYTES else self._compress(directory,target,samples)
        except Exception as exc:
            with self.lock:
                s=self.load(sid)
                s.update(state='error',error='local_decode_failed' if isinstance(exc,(ValueError,OSError)) else 'local_handoff_prepare_failed');self.save(s)
            self.event(sid,'finish_rejected',reason=s['error']);return
        with self.lock:
            s=self.load(sid)
            if s['state']!='handoff':return
            s.update(decoded_sha256=decoded,handoff_file=handoff.name,handoff_bytes=handoff.stat().st_size);self.save(s)
            self._handoff(s,sid,handoff)

    def _compress(self, directory, wav, samples):
        """original.wav -> original.m4a (AAC-LC mono 16 kHz) with the local ffmpeg; verified by ffprobe."""
        target=directory/'original.m4a';temp=directory/'original.m4a.tmp'
        subprocess.run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-i',str(wav),'-map_metadata','-1',
            '-c:a','aac','-b:a',HANDOFF_AAC_BITRATE,'-ac','1','-ar','16000','-movflags','+faststart','-f','mp4',str(temp)],
            check=True,timeout=900,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        probe=json.loads(subprocess.run([FFPROBE,'-v','error','-show_entries','stream=codec_name,sample_rate,channels:format=duration',
            '-of','json',str(temp)],check=True,timeout=120,capture_output=True,stdin=subprocess.DEVNULL).stdout)
        stream=probe['streams'][0]
        if (stream['codec_name'],stream['sample_rate'],stream['channels'])!=('aac','16000',1) or abs(float(probe['format']['duration'])-samples/16000)>0.25:
            raise OSError('compressed_mismatch')
        if not 0<temp.stat().st_size<=STORE_MAX_BYTES: raise OSError('compressed_size')
        os.chmod(temp,0o600)
        with temp.open('rb') as f:os.fsync(f.fileno())
        os.replace(temp,target)
        return target

    def finish(self, sid, count, size, digest):
        with self.lock:
            s=self.expire(self.load(sid))
            self.event(sid,'finish_requested',count=count if type(count) is int and 0<=count<=MAX_SEQ else -1,bytes=size if type(size) is int and 0<=size<=MAX_PCM else -1)
            identity=[count,size,digest]
            if s.get('finish'):
                if s['finish']!=identity: raise GateError('conflicting_finish')
                self.event(sid,'finish_reused');return self.status(sid)
            if s['state']!='recording': raise GateError('not_recording')
            h=hashlib.sha256()
            for n in range(s['next']): h.update((self.directory(sid)/f'{n:06d}.pcm').read_bytes())
            if count!=s['next'] or size!=s['bytes'] or size==0 or digest!=h.hexdigest():
                s.update(state='error',error='incomplete_or_hash_mismatch');self.save(s)
                self.event(sid,'finish_rejected',reason=s['error'])
                raise GateError('incomplete_or_hash_mismatch')
            target=self.directory(sid)/'original.wav'
            with wave.open(str(target),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
                for n in range(count):f.writeframesraw((self.directory(sid)/f'{n:06d}.pcm').read_bytes())
            os.chmod(target,0o600)
            with target.open('rb') as f:os.fsync(f.fileno())
            s.update(finish=identity,state='handoff');self.save(s)
            return self._handoff(s,sid,target)

    def _handoff(self, s, sid, target):
        with self.lock:
            if not self.allow_processing:
                s.update(state='saved',error='processing_not_authorized')
            else:
                try:
                    task,_=self.app.receive_audio(target)
                    s.update(state='processing',task_id=task['id'])
                except GateError:
                    s.update(state='error',error='existing_processing_gate_rejected')
                except Exception:
                    s.update(state='error',error='handoff_unknown_do_not_retry')
            self.save(s);self.event(sid,'finish_result',state=s['state']);return self.status(sid)

    def abort(self,sid,diagnostic=None):
        with self.lock:
            s=self.load(sid)
            self.event(sid,'abort_requested',state_before=s['state'],next=s['next'],bytes=s['bytes'])
            self.save_diagnostic(sid,diagnostic)
            if s['state']=='recording':s.update(state='error',error='device_aborted_incomplete');self.save(s)
            self.event(sid,'abort_result',state=s['state'])
            return self.status(sid)

class DeviceServer(HTTPServer):
    """At most four active handlers; no unbounded ThreadingHTTPServer pool."""
    MaxConnections=4
    MaxCounter=(1<<63)-1
    request_queue_size=4
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(self.MaxConnections)
        self.connections_lock=threading.Lock()
        self.connections={};self.handlers=set();self.connection_serial=0;self.closing=False
        super().__init__(*args,**kwargs)
    def _close_connection(self,request):
        try:self.shutdown_request(request)
        except Exception:
            # Even a failed shutdown must attempt close and cannot skip slot cleanup.
            try:self.close_request(request)
            except Exception:pass
    def process_request(self,request,address):
        acquired=False;handoff=None;thread=None
        try:
            handoff=[False] # allocated inside the protected lifecycle, before admission
            acquired=self.slots.acquire(blocking=False)
            if not acquired:return
            with self.connections_lock:
                try:
                    if self.closing or self.connection_serial==self.MaxCounter:return
                    self.connection_serial+=1
                    self.connections[request]=self.connection_serial
                    thread=threading.Thread(target=self._handle,args=(request,address,handoff),daemon=True)
                    self.handlers.add(thread)
                    try:thread.start()
                    except BaseException:
                        # A wrapper/start failure after native startup still has a worker.
                        handoff[0]=thread.ident is not None
                        raise
                    handoff[0]=True
                finally:
                    if not handoff[0]:
                        # Registration can fail after mutation; don't rely on a registered flag.
                        self.connections.pop(request,None)
                        if thread is not None:self.handlers.discard(thread)
                        self.slots.release();acquired=False
        except Exception:pass # do not log raw request/header/credential data
        finally:
            if handoff is None or not handoff[0]:
                try:self._close_connection(request)
                finally:
                    if acquired:self.slots.release()
    def _handle(self,request,address,handoff):
        with self.connections_lock:
            # A late-starting cancelled target must not touch caller-owned resources.
            if not handoff[0]:return
        try:self.finish_request(request,address)
        except Exception:pass # no raw request/header/credential traceback
        finally:
            try:self._close_connection(request)
            finally:
                with self.connections_lock:
                    self.connections.pop(request,None)
                    self.handlers.discard(threading.current_thread())
                    self.slots.release()
    def server_close(self):
        with self.connections_lock:
            self.closing=True;sockets=list(self.connections);threads=list(self.handlers)
        super().server_close()
        for connection in sockets:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        for thread in threads:thread.join() # socket reads are interrupted; original disk work finishes

class DeviceHandler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*args):pass
    def setup(self):
        super().setup();self.connection.settimeout(3)
        with self.server.connections_lock:self.connection_id=self.server.connections[self.connection]
        self.connection_request=0
    def handle_one_request(self):
        if self.connection_request==self.server.MaxCounter:
            self.close_connection=True;return
        self.chunk_timing=None;self.body_trusted=False;self.connection_request+=1
        return super().handle_one_request()
    def handle_expect_100(self):
        self.send_error(417);self.close_connection=True;return False
    def values(self,name):
        # Real HTTPMessage preserves duplicate fields; dict fallback is for unit doubles.
        return self.headers.get_all(name,[]) if hasattr(self.headers,'get_all') else ([self.headers[name]] if name in self.headers else [])
    def reply(self,code,value):
        timing=getattr(self,'chunk_timing',None);timer=self.server.receiver.timings
        started=timer.now();timer.field(timing,'http',code)
        try:
            self.close_connection=bool(code!=200 or not getattr(self,'body_trusted',False) or self.close_connection or self.server.closing)
            b=json.dumps(value,separators=(',',':')).encode()
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.send_header('Connection','close' if self.close_connection else 'keep-alive');self.send_header('Cache-Control','no-store');self.end_headers()
            try:self.wfile.write(b);timer.field(timing,'reply_ok',True)
            except OSError:self.close_connection=True
        finally:timer.elapsed(timing,'reply_us',started)
    def do_GET(self):self.reply(404,{'error':'not_found'})
    def host_ok(self):
        if not getattr(self.server,'auto_bind',False):return self.headers.get('Host')==self.server.expected_host
        local=self.connection.getsockname()[0]
        return is_lan(local) and is_lan(self.client_address[0]) and self.headers.get('Host')==f'{local}:{self.server.server_port}'
    def do_POST(self):
        self.chunk_timing=None;self.body_trusted=False
        if len(self.values('Host'))!=1 or len(self.values('Authorization'))!=1 or self.values('Origin') or not self.host_ok() or not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+self.server.device_token):
            self.reply(403,{'error':'denied'});return
        early_parts=self.path.split('/')
        action=early_parts[2] if len(early_parts)>=3 else 'unknown'
        event_sid=early_parts[3] if len(early_parts)>=4 and ID.fullmatch(early_parts[3]) else None
        self.chunk_timing=None
        timer=self.server.receiver.timings
        if action=='chunk' and event_sid and len(early_parts)==5 and re.fullmatch(r'[0-9]{1,5}',early_parts[4]) and int(early_parts[4])<MAX_SEQ:
            attempt=self.headers.get('X-Meeting-Attempt','0')
            self.chunk_timing=timer.begin(event_sid,int(early_parts[4]),int(attempt) if attempt in ('1','2') else 0)
            timer.field(self.chunk_timing,'connection_id',getattr(self,'connection_id',0))
            timer.field(self.chunk_timing,'connection_request',getattr(self,'connection_request',0))
        try:
            lengths=self.values('Content-Length')
            if self.values('Transfer-Encoding') or len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,6}',lengths[0]):raise GateError('invalid_body')
            size=int(lengths[0])
            if not 0<=size<=MAX_CHUNK:raise GateError('invalid_body')
            started=timer.now()
            try:body=self.rfile.read(size)
            finally:timer.elapsed(self.chunk_timing,'body_us',started)
            if len(body)!=size:raise GateError('incomplete_body')
            self.body_trusted=True
            parts=self.path.split('/')
            if len(parts) not in (4,5) or parts[1] not in ('v1','v2'):raise GateError('invalid_route')
            codec='opus' if parts[1]=='v2' else 'pcm'
            action,sid=parts[2:4];r=self.server.receiver
            event_sid=sid if ID.fullmatch(sid) else None
            if action=='chunk' and len(parts)==5:
                seq=int(parts[4])
                if not 0<=seq<MAX_SEQ:raise GateError('invalid_sequence')
                started=timer.now()
                try:out=r.chunk(sid,seq,body,self.chunk_timing,codec)
                finally:timer.elapsed(self.chunk_timing,'chunk_us',started)
            elif len(parts)==4 and action=='finish':
                v=json.loads(body)
                out=r.finish_opus(sid,v['count'],v['bytes'],v['sha256'],v['packets'],v['samples']) if codec=='opus' else r.finish(sid,v['count'],v['bytes'],v['sha256'])
            elif len(parts)==4 and action=='abort':
                try: diagnostic=json.loads(body) if body else None
                except (ValueError,UnicodeError):diagnostic=False
                out=r.abort(sid,diagnostic)
            elif len(parts)==4 and action=='start' and not body:out=r.start(sid,codec)
            elif len(parts)==4 and action=='status' and not body:out=r.status(sid)
            else:raise GateError('invalid_route')
            self.reply(200,out)
        except (GateError,ValueError,KeyError,TypeError,OSError) as exc:
            if event_sid:
                reason=str(exc) if isinstance(exc,GateError) and re.fullmatch('[a-z_]{1,64}',str(exc)) else 'invalid_or_incomplete_request'
                self.server.receiver.event(event_sid,'rejected',operation=action if action in ('start','chunk','finish','abort','status') else 'unknown',reason=reason)
            self.reply(409,{'error':'request_rejected_or_incomplete'})
        except Exception:
            if event_sid:self.server.receiver.event(event_sid,'server_error',operation=action if action in ('start','chunk','finish','abort','status') else 'unknown')
            self.reply(500,{'error':'local_failure_no_auto_submit'})
        finally:
            timer.complete(self.chunk_timing)
            # No timing persistence while recording or before the ACK. A late chunk
            # after terminal state also saves its metadata; shutdown flushes after joins.
            if action in ('finish','abort','status','chunk') and event_sid:
                try:
                    if self.server.receiver.load(event_sid)['state']!='recording':timer.flush(event_sid)
                except Exception:pass


def is_lan(ip):
    try: a=ipaddress.ip_address(ip)
    except ValueError: return False
    return a.version==4 and (a.is_loopback or any(a in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')))

def make_server(config,receiver):
    # r6: "auto" listens on every interface so the same config works at home and in
    # the office (and next to a VPN tunnel); each request must still come from a LAN
    # or loopback address and name that connection's own LAN IP:port in Host.
    auto=config['bind']=='auto'
    addr=ipaddress.ip_address('0.0.0.0') if auto else ipaddress.ip_address(config['bind'])
    if not auto and not is_lan(str(addr)):raise ValueError('Use an explicit LAN IPv4 address or "auto"')
    if len(config['token'])<32:raise ValueError('Device credential too short')
    server=DeviceServer((str(addr),config['port']),DeviceHandler)
    server.auto_bind=auto
    server.expected_host=None if auto else f'{addr}:{server.server_port}'
    server.device_token=config['token'];server.receiver=receiver
    server.discovery=DiscoveryResponder(config['token'],server.server_port,config.get('discovery_port',DISCOVERY_PORT)) if config.get('discovery',True) else None
    return server

# r6 discovery (meeting.22): the device broadcasts "XZMEET/1 Q <32 hex nonce>" to UDP
# DISCOVERY_PORT; a receiver holding the device token answers the sender only with
# "XZMEET/1 A <http port> <hex HMAC-SHA256(token, 'XZMEET/1|nonce|port')>". The device
# checks the HMAC before sending anything, so an impostor on the LAN cannot attract
# audio and the token never travels. LAN senders only; bounded reply rate.
DISCOVERY_PORT = 8768
DISCOVERY_QUERY = re.compile(rb'XZMEET/1 Q ([0-9a-f]{32})')

def discovery_answer(token, nonce, port):
    mac=hmac.new(token.encode(),f'XZMEET/1|{nonce}|{port}'.encode(),hashlib.sha256).hexdigest()
    return f'XZMEET/1 A {port} {mac}'.encode()

class DiscoveryResponder:
    MaxRepliesPerSecond=20
    def __init__(self, token, http_port, port=DISCOVERY_PORT):
        self.token,self.http_port,self.port=token,http_port,port
        self.sock=None;self.thread=None;self.stopping=threading.Event();self.window=(0,0)
    def reply_for(self, data, sender):
        m=DISCOVERY_QUERY.fullmatch(data.strip())
        if not m or not is_lan(sender[0]):return None
        second=int(time.monotonic());start,count=self.window
        if second!=start:start,count=second,0
        if count>=self.MaxRepliesPerSecond:return None
        self.window=(start,count+1)
        return discovery_answer(self.token,m.group(1).decode(),self.http_port)
    def start(self):
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.sock.bind(('0.0.0.0',self.port));self.sock.settimeout(0.5)
        self.port=self.sock.getsockname()[1]
        self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()
        return self
    def _run(self):
        while not self.stopping.is_set():
            try:data,sender=self.sock.recvfrom(256)
            except (socket.timeout,OSError):continue
            answer=self.reply_for(data,sender)
            if answer:
                try:self.sock.sendto(answer,sender)
                except OSError:pass
    def stop(self):
        self.stopping.set()
        if self.thread:self.thread.join(2)
        if self.sock:self.sock.close()

def configured_server(app):
    p=Path(__file__).parent/'device.local.json'
    if not p.exists():return None
    if p.stat().st_mode & 0o077:raise ValueError('device.local.json must be mode 600')
    cfg=json.loads(p.read_text())
    return make_server(cfg,DeviceReceiver(app.store.root/'device-sessions',app,cfg.get('allow_processing',False)))
