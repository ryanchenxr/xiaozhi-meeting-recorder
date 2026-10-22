"""Real-time model of the meeting.16 Opus upload path against the real receiver.

Models (not ESP32/lwIP/Wi-Fi): an encoder producing one real libopus packet every
60 ms into a 1 MiB framed ring, and one upload worker with the firmware policy
(min 8 packets, max 64,000 B, retries on fresh connections within 30 s, /v2
routes, finish with packets/samples). The link is modelled on the device side of
a real loopback TCP connection: per-request latency plus bytes/bandwidth delay,
total outages (body never arrives, socket abandoned after the 1.2 s timeout).

meeting.18: finish answers "processing" (decode runs async) and the model then polls
/v2/status every 2 s like the firmware. Scenarios with scale=N run N times faster
than real time: packet period, link latency/bandwidth, retry delays/window, status
polls and the receiver's clock (inactivity/wall caps) are all scaled; the 1.2 s HTTP
socket timeout stays real, so finish must still answer within 1.2 s of real CPU.

meeting.19: start is retried within 15 s (same session id); start/finish/status/abort
use a 5 s socket timeout, chunks keep 1.2 s. start_fail=N swallows the first N start
attempts (body never answered) to model a slow first connect after idle.

meeting.20: policy='m19'|'m20' selects the device constants (ring, chunk cap,
retry window, chunk timeout). stalls=((t0, t1, bytes_per_s), ...) is a near-stall:
a chunk passes only if size/bandwidth fits in one chunk timeout, otherwise the
attempt dies after the timeout (the 2026-10-19 evening pattern: small requests
still passed, ~64 KB chunks never did). This is a model, not lwIP/TCP.

Usage: python3 -I sim_opus.py RECEIVER_DIR [scenario ...]
"""
import hashlib, http.client, json, math, os, struct, sys, tempfile, threading, time
from pathlib import Path

RECEIVER = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(RECEIVER))
import opus_codec as oc                                            # noqa: E402
from device_receiver import DeviceReceiver, make_server            # noqa: E402

TOKEN = 'sim-' + 'x' * 40
MIN_PACKETS, CONTROL_TIMEOUT, START_WINDOW = 8, 5.0, 15.0
POLICIES = {
    'm19': dict(ring=1024 * 1024, cap_max=64000, cap_min=64000, window=240.0, chunk_timeout=1.2),
    'm20': dict(ring=3 * 1024 * 1024, cap_max=16000, cap_min=4000, window=600.0, chunk_timeout=3.0),
}
TONE = oc.encode(b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * (300 + 50 * (i // 16000)) * i / 16000)))
                          for i in range(16000 * 10)), bitrate=32000, cbr=True)  # 10 s, 240 B packets like the firmware

def delay(failed): return 0.1 if failed <= 1 else 0.25 if failed == 2 else 0.5 if failed <= 5 else 1.0

class Link:
    def __init__(self, base=0.1, bandwidth=None, bw_after=None, outages=(), stalls=(), scale=1):
        self.base, self.bw, self.bw_after, self.outages, self.t0, self.scale = base, bandwidth, bw_after, outages, None, scale
        self.stalls = stalls
    def plan(self, now, size, timeout=1.2):
        rel = (now - self.t0) * self.scale                         # model seconds
        if any(a <= rel < b for a, b in self.outages): return 'blackhole', 0
        for a, b, bw in self.stalls:
            if a <= rel < b:
                t = size / bw
                return ('blackhole', 0) if t > timeout else ('ok', (self.base + t) / self.scale)
        bw = self.bw_after[1] if self.bw_after and rel >= self.bw_after[0] else self.bw
        return 'ok', (self.base + (size / bw if bw else 0)) / self.scale

class Device:
    def __init__(self, port, link, seconds, scale=1, start_fail=0, policy='m20'):
        self.port, self.link, self.seconds, self.k, self.start_fail = port, link, seconds, 1.0 / scale, start_fail
        self.p = POLICIES[policy]; self.policy = policy; self.min_cap = self.p['cap_max']
        self.start_attempts = 0
        self.sid = os.urandom(16).hex(); self.ring = []; self.ring_bytes = 0; self.lock = threading.Lock()
        self.done = False; self.error = None; self.conn = None; self.requests = 0; self.max_ring = 0; self.chunks = []
        self.finish_ms = None; self.polls = 0
    def encoder(self):
        t = time.monotonic()
        for i in range(self.seconds * 1000 // 60):
            p = TONE[i % len(TONE)]
            with self.lock:
                if self.ring_bytes + 2 + len(p) > self.p['ring']: self.error = self.error or f'RingFull at {i*0.06:.1f}s'; return
                self.ring.append(p); self.ring_bytes += 2 + len(p); self.max_ring = max(self.max_ring, self.ring_bytes)
            t += 0.06 * self.k; d = t - time.monotonic()
            if d > 0: time.sleep(d)
        with self.lock: self.done = True
    def reset(self):
        if self.conn:
            try: self.conn.close()
            except OSError: pass
        self.conn = None
    def request(self, path, body):
        self.requests += 1
        chunk = '/chunk/' in path
        action, wait = self.link.plan(time.monotonic(), len(body), self.p['chunk_timeout']) if chunk else ('ok', 0)
        if '/start/' in path:
            self.start_attempts += 1
            if self.start_attempts <= self.start_fail: action = 'blackhole'
        limit = self.p['chunk_timeout'] if chunk else CONTROL_TIMEOUT
        try:
            if self.conn is None: self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=limit + wait)
            self.conn.timeout = limit + wait
            if self.conn.sock: self.conn.sock.settimeout(limit + wait)
            headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Length': str(len(body))}
            if action == 'blackhole':
                self.conn.putrequest('POST', path); [self.conn.putheader(k, v) for k, v in headers.items()]
                self.conn.endheaders(); self.conn.send(body[:len(body) // 2]); time.sleep(limit * self.k); raise TimeoutError
            time.sleep(wait)
            self.conn.request('POST', path, body=body, headers=headers)
            r = self.conn.getresponse(); data = r.read()
            if r.status != 200 or r.getheader('Connection') == 'close': self.reset()
            return r.status == 200, data
        except (OSError, http.client.HTTPException, TimeoutError):
            self.reset(); return False, b''
    def worker(self):
        first, failed = time.monotonic(), 0
        ok, data = self.request(f'/v2/start/{self.sid}', b'')
        while not ok:                                                              # meeting.19 start retry
            failed += 1
            if failed >= 20 or time.monotonic() - first + delay(failed) * self.k >= START_WINDOW * self.k: return 'start_failed', 0, 0
            time.sleep(delay(failed) * self.k); ok, data = self.request(f'/v2/start/{self.sid}', b'')
        assert json.loads(data)['state'] == 'recording'
        self.link.t0 = time.monotonic(); threading.Thread(target=self.encoder, daemon=True).start()
        count = size = packets = 0; h = hashlib.sha256(); cap = self.p['cap_max']
        while self.error is None:
            for _ in range(25):
                with self.lock: n, done = len(self.ring), self.done
                if n >= MIN_PACKETS or done: break
                time.sleep(0.02 * self.k)
            with self.lock:
                take, used = [], 0
                while self.ring and used + 2 + len(self.ring[0]) <= cap:
                    p = self.ring.pop(0); take.append(p); used += 2 + len(p); self.ring_bytes -= 2 + len(p)
                done = self.done
            if take:
                body = oc.frame(take); path = f'/v2/chunk/{self.sid}/{count}'; first = time.monotonic(); failed = 0
                ok, data = self.request(path, body)
                while not ok and self.error is None:
                    failed += 1
                    if time.monotonic() - first + delay(failed) * self.k >= self.p['window'] * self.k: break
                    time.sleep(delay(failed) * self.k); ok, data = self.request(path, body)
                if ok: ok = json.loads(data)['next'] == count + 1
                if not ok: self.error = self.error or f'HttpFinal seq={count}'; break
                cap = max(self.p['cap_min'], cap // 2) if failed else min(self.p['cap_max'], cap * 2)   # meeting.20 NextChunkCap
                self.min_cap = min(self.min_cap, cap)
                h.update(body); size += len(body); count += 1; packets += len(take); self.chunks.append(len(take))
            with self.lock:
                if done and not self.ring: break
        if self.error: self.request(f'/v2/abort/{self.sid}', b''); return 'aborted', count, packets
        samples = packets * 960
        body = json.dumps({'count': count, 'bytes': size, 'sha256': h.hexdigest(), 'packets': packets, 'samples': samples}).encode()
        t = time.monotonic(); ok, data = self.request(f'/v2/finish/{self.sid}', body)
        if not ok: ok, data = self.request(f'/v2/finish/{self.sid}', body)          # firmware: 2 attempts
        self.finish_ms = round((time.monotonic() - t) * 1000)
        if not ok: return 'finish_failed', count, packets
        state, failures = json.loads(data)['state'], 0
        while state == 'processing':                                                # firmware status loop
            time.sleep(2 * self.k); ok, data = self.request(f'/v2/status/{self.sid}', b''); self.polls += 1
            if ok: state, failures = json.loads(data)['state'], 0
            elif (failures := failures + 1) >= 3: return 'status_failed', count, packets
        return state, count, packets

SCENARIOS = {
    # Field 2026-10-19 meeting.15: link fell from ~60 KB/s to ~8.5 KB/s within seconds.
    'collapse_to_8k': dict(link=dict(base=0.25, bandwidth=60000, bw_after=(8, 8500)), seconds=120),
    # Link slower than the codec's nominal rate for the whole session.
    'link_3k': dict(link=dict(base=0.25, bandwidth=3000), seconds=90),
    # 25 s total outage: absorbed by the ring and the 30 s retry window.
    'outage_25s': dict(link=dict(base=0.2, bandwidth=20000, outages=((20, 45),)), seconds=90),
    # 60 s outage (killed meeting.16's 30 s window in the field-like case): now absorbed.
    'outage_60s': dict(link=dict(base=0.2, bandwidth=20000, outages=((20, 80),)), seconds=90),
    # 300 s outage: beyond the 240 s window, must end incomplete (never saved).
    'outage_300s': dict(link=dict(base=0.2, bandwidth=20000, outages=((10, 310),)), seconds=330),
    # meeting.20: the 2026-10-19 evening pattern, 60 min, uplink near-stalls at 1.5 KB/s
    # for 6 minutes from minute 23 (attempt 1). m19 policy must die (RingFull), m20 must save.
    'stall6_m19_x20': dict(link=dict(base=0.25, bandwidth=20000, stalls=((1380, 1740, 1500),)), seconds=3600, scale=20, policy='m19'),
    'stall6_m20_x20': dict(link=dict(base=0.25, bandwidth=20000, stalls=((1380, 1740, 1500),)), seconds=3600, scale=20, policy='m20'),
    # Two stalls like attempts 1+2 (minute 6 and minute 23), m20.
    'stall2x_m20_x20': dict(link=dict(base=0.25, bandwidth=20000, stalls=((380, 740, 1500), (1380, 1740, 1500))), seconds=3600, scale=20, policy='m20'),
    # Total 9 min outage (inside the 600 s chunk window and the ~13 min ring), m20.
    'outage9_m20_x20': dict(link=dict(base=0.2, bandwidth=20000, outages=((1200, 1740),)), seconds=3600, scale=20, policy='m20'),
    # meeting.19: first two start attempts swallowed (slow first connect after idle).
    'start_slow_connect': dict(link=dict(base=0.2, bandwidth=20000), seconds=30, start_fail=2),
    # Full 10-minute limit on the collapsed link.
    'full_600_8k': dict(link=dict(base=0.25, bandwidth=8500), seconds=600),
    # meeting.18: full 60-minute limit on the collapsed link, 20x faster than real time.
    'full_3600_8k_x20': dict(link=dict(base=0.25, bandwidth=8500), seconds=3600, scale=20),
    # 60 minutes with a 120 s outage at minute 30 (ring ~260 s at nominal), 20x.
    'full_3600_outage120_x20': dict(link=dict(base=0.2, bandwidth=20000, outages=((1800, 1920),)), seconds=3600, scale=20),
}

def run(name):
    spec = SCENARIOS[name]
    with tempfile.TemporaryDirectory() as tmp:
        class App: store = None
        scale = spec.get('scale', 1); base = time.time()
        rcv = DeviceReceiver(tmp, App(), allow_processing=False, clock=lambda: base + (time.time() - base) * scale)
        srv = make_server({'bind': '127.0.0.1', 'port': 0, 'token': TOKEN}, rcv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        dev = Device(srv.server_port, Link(**spec['link'], scale=scale), spec['seconds'], scale, spec.get('start_fail', 0), spec.get('policy', 'm20'))
        t0 = time.monotonic(); state, count, packets = dev.worker(); wall = time.monotonic() - t0
        srv.shutdown(); srv.server_close(); s = rcv.join_finish(dev.sid)
        wav = Path(tmp, dev.sid, 'original.wav'); frames = None
        if wav.exists():
            import wave
            with wave.open(str(wav)) as w: frames = w.getnframes()
        full = packets == spec['seconds'] * 1000 // 60
        res = dict(scenario=name, device=state, receiver=s['state'], error=dev.error, seconds=spec['seconds'],
                   packets=packets, chunks=count, requests=dev.requests, max_ring_kb=round(dev.max_ring / 1024, 1),
                   max_chunk_packets=max(dev.chunks or [0]), wav_frames=frames, wall_s=round(wall, 1),
                   scale=scale, model_s=round(wall * scale, 1), finish_ms=dev.finish_ms, status_polls=dev.polls,
                   start_attempts=dev.start_attempts, policy=dev.policy, min_cap=dev.min_cap)
        if s['state'] == 'saved': assert full and frames == packets * 960, res
        else: assert frames is None, res
        return res

if __name__ == '__main__':
    names = sys.argv[2:] or [n for n in SCENARIOS if n not in ('full_600_8k', 'outage_300s') and 'scale' not in SCENARIOS[n]]
    out = []; ts = [threading.Thread(target=lambda n=n: out.append(run(n))) for n in names]
    [t.start() for t in ts]; [t.join() for t in ts]
    out.sort(key=lambda r: names.index(r['scenario'])); print(json.dumps(out, ensure_ascii=False, indent=1))
