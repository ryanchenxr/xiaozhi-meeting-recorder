"""r6 / meeting.22: receiver discovery (UDP + HMAC proof) and bind "auto". Loopback only."""
import hashlib, hmac, http.client, json, socket, tempfile, threading, unittest
from device_receiver import DeviceReceiver, DiscoveryResponder, discovery_answer, make_server, is_lan
TOKEN='k'*40; NONCE='0123456789abcdef0123456789abcdef'
class App: store=None
class DiscoveryTests(unittest.TestCase):
    def test_answer_is_hmac_over_nonce_and_port(self):
        a=discovery_answer(TOKEN,NONCE,8767).decode().split()
        self.assertEqual(a[:3],['XZMEET/1','A','8767'])
        self.assertEqual(a[3],hmac.new(TOKEN.encode(),f'XZMEET/1|{NONCE}|8767'.encode(),hashlib.sha256).hexdigest())
        self.assertNotIn(TOKEN,' '.join(a))                                        # the token never travels
        self.assertNotEqual(a[3],discovery_answer('x'*40,NONCE,8767).decode().split()[3])
    def test_filters_and_rate_limit(self):
        r=DiscoveryResponder(TOKEN,8767,0)
        for bad in (b'', b'XZMEET/1 Q short', b'XZMEET/2 Q '+NONCE.encode(), b'XZMEET/1 Q '+NONCE.upper().encode()):
            self.assertIsNone(r.reply_for(bad,('192.168.1.9',5000)))
        for outside in ('8.8.8.8','100.64.0.1','198.18.1.135'):                    # not LAN: no answer
            self.assertIsNone(r.reply_for(b'XZMEET/1 Q '+NONCE.encode(),(outside,5000)))
        answers=[r.reply_for(b'XZMEET/1 Q '+NONCE.encode(),('10.1.2.3',5000)) for _ in range(30)]
        self.assertEqual(sum(a is not None for a in answers),DiscoveryResponder.MaxRepliesPerSecond)
    def test_udp_round_trip_on_loopback(self):
        r=DiscoveryResponder(TOKEN,8767,0).start()
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2)
            s.sendto(b'XZMEET/1 Q '+NONCE.encode(),('127.0.0.1',r.port))
            data,sender=s.recvfrom(256);self.assertEqual(data,discovery_answer(TOKEN,NONCE,8767))
            s.sendto(b'garbage',('127.0.0.1',r.port));s.settimeout(0.4)
            with self.assertRaises(socket.timeout):s.recvfrom(256)
            s.close()
        finally: r.stop()
    def test_lan_classification(self):
        self.assertTrue(all(map(is_lan,('192.168.1.128','10.0.0.2','172.20.1.1','127.0.0.1'))))
        self.assertFalse(any(map(is_lan,('8.8.8.8','198.18.1.135','172.32.0.1','0.0.0.0','::1','x'))))
class AutoBindTests(unittest.TestCase):
    def test_host_must_be_connection_local_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv=make_server({'bind':'auto','port':0,'token':TOKEN,'discovery':False},DeviceReceiver(tmp,App()))
            self.assertIsNone(srv.discovery)
            t=threading.Thread(target=srv.serve_forever,kwargs={'poll_interval':.01});t.start()
            def post(host):
                c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=5)
                c.request('POST','/v2/status/'+'a'*32,b'',{'Host':host,'Authorization':'Bearer '+TOKEN});r=c.getresponse();r.read();c.close();return r.status
            try:
                self.assertEqual(post(f'127.0.0.1:{srv.server_port}'),409)            # authorised; unknown session
                for host in ('evil.invalid',f'192.168.1.128:{srv.server_port}',f'127.0.0.1:{srv.server_port+1}',f'localhost:{srv.server_port}'):
                    self.assertEqual(post(host),403,host)
            finally: srv.shutdown();srv.server_close();t.join()
    def test_explicit_bind_unchanged_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv=make_server({'bind':'127.0.0.1','port':0,'token':TOKEN},DeviceReceiver(tmp,App()))
            self.assertEqual(srv.expected_host,f'127.0.0.1:{srv.server_port}');self.assertIsNotNone(srv.discovery);srv.server_close()
            for bad in ('8.8.8.8','0.0.0.0'):
                with self.assertRaises(ValueError):make_server({'bind':bad,'port':0,'token':TOKEN},DeviceReceiver(tmp,App()))
if __name__=='__main__': unittest.main()
