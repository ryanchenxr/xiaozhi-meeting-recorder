"""Explicit LOCAL SIMULATOR. Opens only the device listener, never App/Chrome/provider.
Uses current LAN config, saves synthetic original audio in an isolated evidence run.
"""
import hashlib, http.client, json, os, threading, time, uuid, wave
from pathlib import Path
from device_receiver import DeviceReceiver, make_server
from store import atomic
class NoProvider:
    def receive_audio(self,*args):raise AssertionError('Simulator must never submit audio')
def main():
    os.umask(0o077)
    root=Path(__file__).resolve().parent
    cfg=json.loads((root/'device.local.json').read_text())
    run=root/'evidence'/('local-simulator-'+time.strftime('%Y%m%d-%H%M%S'))
    run.mkdir(mode=0o700)
    receiver=DeviceReceiver(run/'sessions',NoProvider(),False)
    server=make_server(cfg,receiver)
    t=threading.Thread(target=server.serve_forever);t.start()
    def call(action,sid,data=b''):
        conn=http.client.HTTPConnection(cfg['bind'],cfg['port'],timeout=5)
        conn.request('POST','/v1/'+action+'/'+sid,data,{'Authorization':'Bearer '+cfg['token']})
        r=conn.getresponse();body=json.loads(r.read());conn.close()
        return r.status,body
    try:
        sid=uuid.uuid4().hex;assert call('start',sid)[0]==200
        # Distinct alternating signed PCM samples; not speech and not a microphone.
        chunk=b'\x00\x10\x00\xf0'*800
        for n in range(20):
            status,state=call('chunk',sid+'/'+str(n),chunk);assert status==200 and state['next']==n+1
            if n==3:assert call('chunk',sid+'/'+str(n),chunk)==(status,state)
        body=json.dumps(dict(count=20,bytes=64000,sha256=hashlib.sha256(chunk*20).hexdigest())).encode()
        done=call('finish',sid,body);assert done[0]==200 and done[1]['state']=='saved'
        assert call('finish',sid,body)==done
        with wave.open(str(run/'sessions'/sid/'original.wav')) as f:assert f.readframes(32000)==chunk*20
        broken=uuid.uuid4().hex;call('start',broken);call('chunk',broken+'/0',chunk)
        assert call('chunk',broken+'/2',chunk)[0]==409
        bad=json.dumps(dict(count=3,bytes=9600,sha256='0'*64)).encode()
        assert call('finish',broken,bad)[0]==409
        assert call('status',broken)[1]['state']=='error'
        atomic(run/'result.json',dict(label='LOCAL SYNTHETIC SIMULATOR, NOT DEVICE AUDIO',transport='same Mac through configured LAN IPv4 listener',sample_seconds=2,pcm_bytes=64000,wav_sha256=hashlib.sha256((run/'sessions'/sid/'original.wav').read_bytes()).hexdigest(),duplicate_chunk=True,duplicate_finish=True,missing_chunk_rejected=True,incomplete_not_submitted=True,provider_calls=0,device_used=False))
        print('PASS: local synthetic LAN protocol; 0 provider calls; 0 device access. Evidence: '+run.name)
    finally:server.shutdown();server.server_close();t.join()
if __name__=='__main__':main()
