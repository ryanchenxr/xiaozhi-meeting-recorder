#pragma once
#include <cstddef>
#include <cstdint>

// Portable upload budget; used by MeetingRecorder and native regression.
//
// meeting.16 encodes on the device: 16 kHz mono Opus, 60 ms packets, ~32 kbit/s
// (~4,000 B/s instead of 32,000 B/s PCM). Each request still carries everything
// queued (at least ~0.5 s, at most 64,000 B), so a slow request is followed by a
// larger one. A 1 MiB PSRAM ring of encoded packets absorbs link stalls of
// minutes rather than seconds; the 15 s PCM queue now only covers encoder jitter.
namespace MeetingUpload {
constexpr uint32_t FrameSamples=160;              // 10 ms capture frame (unchanged)
constexpr uint32_t PcmBytesPerSecond=16000*2;
constexpr uint32_t OpusFrameSamples=960;          // 60 ms
constexpr uint32_t OpusFramesPerPacket=OpusFrameSamples/FrameSamples;
constexpr uint32_t OpusBitrate=32000;             // VBR target, bit/s
constexpr uint32_t OpusNominalBytesPerSecond=OpusBitrate/8;
constexpr uint32_t MinChunkPackets=8;              // ~0.48 s when caught up (unchanged)
constexpr size_t MaxChunkBytes=64000;              // receiver MAX_CHUNK and send buffer (unchanged)
constexpr uint32_t QueueFrames=1500;               // PCM queue, 15 s, ~483 KB PSRAM (unchanged)
// meeting.20: two 60 min runs (2026-10-19 evening, RSSI -76..-81) died of RingFull
// when the uplink nearly stalled for 5-6 minutes: small requests still passed in
// <1 s, but every backlogged ~64 KB chunk failed in write and restarted from zero.
// The ring now covers ~13 minutes (PSRAM had ~5.2 MB free while recording with
// the 1 MiB ring), new chunks are capped adaptively (see NextChunkCap) and the
// chunk write timeout lets TCP retransmit instead of resetting after 1.2 s.
constexpr size_t OpusRingBytes=3*1024*1024;        // ~786 s at the nominal rate
// A chunk (same seq, same bytes) is retried on fresh connections until it has
// been outstanding this long; meanwhile capture keeps filling the ring. Receiver
// inactivity is 630 s (r5).
constexpr int64_t ChunkRetryWindowUs=600000000;
// New chunks (never a retry, which must resend identical bytes) are capped: 16 KB
// when the link is healthy, halved after any failed attempt down to 4 KB, doubled
// after a chunk that passed on its first attempt. 4 KB is ~1 s of audio, so the
// cap alone never limits throughput below the codec rate at <=1 request/s.
constexpr size_t ChunkCapMaxBytes=16000;
constexpr size_t ChunkCapMinBytes=4000;
inline size_t NextChunkCap(size_t cap,bool chunk_failed_any_attempt) {
    if(chunk_failed_any_attempt)return cap/2<ChunkCapMinBytes?ChunkCapMinBytes:cap/2;
    return cap*2>ChunkCapMaxBytes?ChunkCapMaxBytes:cap*2;
}

// Per-phase HTTP timeouts (per blocking operation, not per request). meeting.19 used
// 1.2 s for chunks: barely one TCP retransmission, so on a lossy link each attempt
// was torn down mid-write (err_kind=3, 2.5-14 s per attempt). meeting.20: 3 s.
// Control requests are single round trips whose latency on the home link reached
// 1.20 s for finish and whose first connect after idle once exceeded 1.2 s: 5 s.
constexpr int ChunkTimeoutMs=3000;
constexpr int ControlTimeoutMs=5000;
// Start was a single attempt; one slow first connect ended a session before any
// audio. Capture is enabled only after start succeeds, so retrying loses nothing.
// The receiver's start is idempotent (start_reused) for the same session id.
constexpr int64_t StartRetryWindowUs=15000000;

inline uint32_t RetryDelayMs(uint32_t failed_attempts) {
    return failed_attempts<=1?100u:(failed_attempts==2?250u:(failed_attempts<=5?500u:1000u));
}
// Another attempt is useful only while the receiver can still accept this seq and
// capture has not failed (the caller checks state). Any failure is retried: the
// receiver dedups same seq+bytes and a rejected chunk never changes its state.
inline bool ShouldRetry(int64_t first_started_us,int64_t now_us,uint32_t failed_attempts) {
    if(failed_attempts>=250)return false; // uint8_t attempt counter in diagnostics
    return now_us-first_started_us+int64_t(RetryDelayMs(failed_attempts))*1000<ChunkRetryWindowUs;
}
inline bool ShouldRetryStart(int64_t first_started_us,int64_t now_us,uint32_t failed_attempts) {
    if(failed_attempts>=20)return false;
    return now_us-first_started_us+int64_t(RetryDelayMs(failed_attempts))*1000<StartRetryWindowUs;
}
static_assert(OpusFramesPerPacket*FrameSamples==OpusFrameSamples,"whole capture frames per packet");
static_assert(ControlTimeoutMs>ChunkTimeoutMs,"control requests get a longer timeout");
static_assert(ChunkCapMinBytes>=2+1275 && ChunkCapMaxBytes<=MaxChunkBytes && ChunkCapMinBytes<=ChunkCapMaxBytes,"a capped chunk still fits one maximum Opus record");
static_assert(ChunkCapMinBytes>=OpusNominalBytesPerSecond,"4 KB chunk carries >= 1 s of audio");
static_assert(57600000%OpusFrameSamples==0,"3600 s limit ends on a packet boundary");
// 60 min = 57,600,000 samples = 60,000 packets; every counter (samples, packets,
// framed bytes ~15 MB, chunk seq ~7,500) is uint32_t with ample headroom.
static_assert(3600ull*16000<=UINT32_MAX && 3600ull*OpusNominalBytesPerSecond*2<=UINT32_MAX,"60 min fits uint32 counters");
// Ring holds the whole retry window at the nominal rate (measured VBR average was
// ~3.2 KB/s, below nominal); a longer stall ends explicitly as RingFull.
static_assert(OpusRingBytes>=1ull*OpusNominalBytesPerSecond*(ChunkRetryWindowUs/1000000),"ring must outlast a retry window");
}
