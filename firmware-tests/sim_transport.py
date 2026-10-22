"""Real-time model of the device upload worker against the real receiver.

Models (not ESP32/lwIP/Wi-Fi): a 32,000 B/s producer feeding a bounded frame
queue, and one upload worker using either the meeting.13 algorithm (10 frames,
2 s queue, one retry) or the meeting.14 algorithm (batch 10..200 frames, 15 s
queue, retries on fresh connections within 8 s). Faults are injected on the
device side of a real loopback TCP connection to the unmodified-except-limits
receiver: added per-request latency, stalls where the body never arrives (the
socket is abandoned), and lost ACKs after the receiver persisted the chunk.

Usage: python3 -I sim_transport.py RECEIVER_DIR [scenario ...]
"""
import hashlib, http.client, json, os, queue, random, socket, sys, tempfile, threading, time
from pathlib import Path

RECEIVER = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(RECEIVER))
from device_receiver import DeviceReceiver, make_server, MAX_CHUNK  # noqa: E402

TOKEN = 'sim-' + 'x' * 40  # fictitious
FRAME_BYTES = 320

class App:  # finish() only consults the store when processing is allowed
    store = None

class Algo:
    def __init__(self, name, queue_frames, min_frames, max_frames, retry):
        self.name, self.queue_frames, self.min_frames, self.max_frames, self.retry = name, queue_frames, min_frames, max_frames, retry

OLD = Algo('meeting.13', 200, 10, 10, 'once')
NEW = Algo('meeting.14', 1500, 10, 200, 'window')

def retry_delay(failed):  # meeting_upload_policy.h RetryDelayMs
    return 0.1 if failed <= 1 else (0.25 if failed == 2 else 0.5)

def should_retry(first, now, failed):  # meeting_upload_policy.h ShouldRetry
    return failed < 250 and (now - first + retry_delay(failed)) < 8.0

class Faults:
    """Deterministic per-scenario fault plan, evaluated per request attempt."""
    def __init__(self, base=0.0, jitter=0.0, stall_every=None, stall_for=0.0, ack_loss_every=None, outage=None, seed=1):
        self.base, self.jitter, self.stall_every, self.stall_for = base, jitter, stall_every, stall_for
        self.ack_loss_every, self.outage, self.rng, self.n = ack_loss_every, outage, random.Random(seed), 0
        self.t0 = None
    def plan(self, now):
        self.n += 1
        rel = now - self.t0
        if self.outage and self.outage[0] <= rel < self.outage[1]:
            return 'blackhole', 0.0
        if self.stall_every and int(rel / self.stall_every) > 0 and (rel % self.stall_every) < self.stall_for:
            return 'blackhole', 0.0
        if self.ack_loss_every and self.n % self.ack_loss_every == 0:
            return 'ack_loss', 0.0
        return 'ok', self.base + self.rng.random() * self.jitter

class Device:
    def __init__(self, algo, port, faults, seconds):
        self.algo, self.port, self.faults, self.seconds = algo, port, faults, seconds
        self.q = queue.Queue(maxsize=algo.queue_frames)
        self.error = None; self.sent = hashlib.sha256(); self.produced = hashlib.sha256()
        self.conn = None; self.requests = 0; self.max_queue = 0; self.chunks = []
        self.stop = threading.Event(); self.produced_frames = 0
        self.sid = os.urandom(16).hex()

    def producer(self):
        total = self.seconds * 100; t = time.monotonic()
        for i in range(total):
            frame = hashlib.sha256(f'{self.sid}:{i}'.encode()).digest() * 10  # 320 B
            self.produced.update(frame)
            try: self.q.put_nowait(frame)
            except queue.Full:
                self.error = self.error or f'QueueFull at {i/100:.2f}s'; return
            self.produced_frames += 1
            self.max_queue = max(self.max_queue, self.q.qsize())
            t += 0.01; d = t - time.monotonic()
            if d > 0: time.sleep(d)
        self.stop.set()

    def reset(self):
        if self.conn:
            try: self.conn.close()
            except OSError: pass
        self.conn = None

    def request(self, path, body):
        self.requests += 1
        action, delay = self.faults.plan(time.monotonic()) if '/chunk/' in path else ('ok', 0.0)
        try:
            if self.conn is None:
                self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1.2)
            time.sleep(delay)  # added link/stack latency for this request
            headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/octet-stream', 'Content-Length': str(len(body))}
            if action == 'blackhole':
                # Headers and part of the body leave; the rest never arrives. Like the
                # device, wait out the 1.2 s response timeout, then abandon the socket.
                self.conn.putrequest('POST', path); [self.conn.putheader(k, v) for k, v in headers.items()]
                self.conn.endheaders(); self.conn.send(body[: len(body) // 2]); time.sleep(1.2)
                raise TimeoutError('blackhole')
            self.conn.request('POST', path, body=body, headers=headers)
            if action == 'ack_loss':
                time.sleep(0.05); raise TimeoutError('ack lost')  # receiver persisted; reply never read
            r = self.conn.getresponse(); data = r.read()
            if r.status != 200 or r.getheader('Connection') == 'close': self.reset()
            return r.status == 200, data
        except (OSError, http.client.HTTPException, TimeoutError):
            self.reset(); return False, b''

    def worker(self):
        ok, data = self.request(f'/v1/start/{self.sid}', b'')
        assert ok and json.loads(data)['state'] == 'recording'
        self.faults.t0 = time.monotonic()
        threading.Thread(target=self.producer, daemon=True).start()
        count = bytes_ = 0
        while self.error is None:
            frames = []
            while len(frames) < self.algo.max_frames:
                try: frames.append(self.q.get(timeout=0.02) if len(frames) < self.algo.min_frames else self.q.get_nowait())
                except queue.Empty: break
            if frames:
                block = b''.join(frames); path = f'/v1/chunk/{self.sid}/{count}'
                first = time.monotonic(); failed = 0
                ok, data = self.request(path, block)
                if self.algo.retry == 'once':
                    if not ok: ok, data = self.request(path, block)
                else:
                    while not ok and self.error is None:
                        failed += 1
                        if not should_retry(first, time.monotonic(), failed): break
                        time.sleep(retry_delay(failed)); ok, data = self.request(path, block)
                if ok:
                    ack = json.loads(data); ok = ack['next'] == count + 1 and ack['session'] == self.sid
                if not ok:
                    self.error = self.error or f'HttpFinal seq={count}'; break
                self.sent.update(block); bytes_ += len(block); count += 1; self.chunks.append(len(frames))
            if self.stop.is_set() and self.q.empty(): break
        if self.error is None:
            body = json.dumps({'count': count, 'bytes': bytes_, 'sha256': self.sent.hexdigest()}).encode()
            ok, data = self.request(f'/v1/finish/{self.sid}', body)
            if not ok: ok, data = self.request(f'/v1/finish/{self.sid}', body)
            return (json.loads(data)['state'] if ok else 'finish_failed'), count, bytes_
        self.request(f'/v1/abort/{self.sid}', b'')
        return 'aborted', count, bytes_

SCENARIOS = {
    # meeting.13 + r1 field: requests ~195-280 ms each.
    'r1_latency': dict(faults=dict(base=0.19, jitter=0.09), seconds=30),
    # Receiver-B field: ~620 ms request, then two attempts whose body never arrived.
    'b_stall': dict(faults=dict(base=0.30, jitter=0.35, stall_every=10.0, stall_for=2.5), seconds=30),
    # Lost ACK after persist on every 7th request: dedup path, never double-appends.
    'ack_loss': dict(faults=dict(base=0.10, jitter=0.20, ack_loss_every=7), seconds=20),
    # Genuine 12 s outage: must end incomplete, never saved/submitted.
    'outage_12s': dict(faults=dict(base=0.05, outage=(5.0, 17.0)), seconds=25),
    # Full 3-minute limit under r1-like latency, a B-like stall every 30 s and lost
    # ACKs: drain at the end, finish hash, and the receiver's session wall-clock cap.
    'full_180': dict(faults=dict(base=0.19, jitter=0.35, stall_every=30.0, stall_for=2.5, ack_loss_every=40), seconds=180),    # meeting.15: full 10-minute limit under the same field-like faults.
    'full_600': dict(faults=dict(base=0.19, jitter=0.35, stall_every=30.0, stall_for=2.5, ack_loss_every=40), seconds=600),
}

def run(name, algo):
    spec = SCENARIOS[name]
    with tempfile.TemporaryDirectory() as tmp:
        receiver = DeviceReceiver(tmp, App(), allow_processing=False)
        server = make_server({'bind': '127.0.0.1', 'port': 0, 'token': TOKEN}, receiver)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        dev = Device(algo, server.server_port, Faults(**spec['faults']), spec['seconds'])
        started = time.monotonic(); state, count, size = dev.worker(); elapsed = time.monotonic() - started
        server.shutdown(); server.server_close()
        s = receiver.status(dev.sid)
        files = sorted(Path(tmp, dev.sid).glob('*.pcm'))
        stored = hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
        wav = Path(tmp, dev.sid, 'original.wav').exists()
        full = dev.produced_frames == spec['seconds'] * 100
        result = dict(scenario=name, algo=algo.name, device=state, receiver=s['state'], error=dev.error,
                      seconds=spec['seconds'], stored_s=round(s['bytes'] / 32000, 2), chunks=count,
                      requests=dev.requests, max_queue_s=round(dev.max_queue / 100, 2),
                      max_chunk_frames=max(dev.chunks or [0]), wall_s=round(elapsed, 1), wav=wav,
                      integrity=(stored == dev.produced.hexdigest()) if full and state == 'saved' else None)
        # Never a false PASS: saved requires full audio, matching hash and a WAV.
        if s['state'] == 'saved': assert full and wav and stored == dev.produced.hexdigest(), result
        else: assert not wav, result
        return result

if __name__ == '__main__':
    names = sys.argv[2:] or list(SCENARIOS)
    results = []; threads = []
    def go(n, a): results.append(run(n, a))
    for n in names:
        for a in (OLD, NEW):
            t = threading.Thread(target=go, args=(n, a)); t.start(); threads.append(t)
    for t in threads: t.join()
    results.sort(key=lambda r: (names.index(r['scenario']), r['algo']))
    print(json.dumps(results, ensure_ascii=False, indent=1))
