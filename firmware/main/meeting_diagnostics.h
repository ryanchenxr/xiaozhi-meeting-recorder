#pragma once
#include <cstdint>
#include <cstring>
#include <cstddef>
#include "meeting_request_timing.h"

// Fixed POD only. Recorder mutex guards all access; capture performs no allocation/I/O.
struct MeetingDiagnostics {
    enum Reason : uint8_t { None, InputRead, Resample, PcmInvalid, QueueFull, TailFlush,
        HttpFinal, AckInvalid, NoInputProgress, DrainMismatch, FinishHttp, FinishResponse,
        StartResponse, EmptyStop, StatusHttp, StatusResponse, TaskCreate, EncodeFailed, RingFull };
    enum Phase : uint8_t { NoPhase, StartHttp, ChunkHttp, FinishHttpPhase, AbortHttp, StatusHttpPhase };
    enum Source : uint8_t { Unknown, Mcp, DirectVoice, Boot, Limit };
    enum EventKind : uint8_t { StartAccepted, StartReused, StartAllocationFailed, StopRequested,
        AutoStop, HttpAttemptFailed, HttpRetryRecovered, FatalLatched, AbortCleanup, WorkerEnded, StateChanged };
    struct Request {
        uint64_t started_us=0, elapsed_us=0;
        uint32_t seq=0, payload_written=0, expected_next=0;
        int32_t code=0, http=0, actual_next=-1;
        uint8_t phase=NoPhase, attempt=0, error_kind=0, ack=0; // ack:0 n/a,1 valid,2 invalid
        bool in_flight=false,ack_session_match=false;
    };
    struct Context {
        uint32_t sampled=0,enqueued=0,dequeued=0,tx_pcm_bytes=0,acked_bytes=0,acked_chunks=0,seq=0,queue_frames=0;
        uint64_t stop_us=0;
        uint32_t stop_requests=0;
        int32_t capture_code=0;
        uint8_t state=0,stop_source=Unknown;
        Request request;
    } current;
    MeetingTimingHistory timing;
    struct Fault { bool present=false; uint8_t reason=None; uint64_t time_us=0; Context context; } first;
    struct Event { uint64_t time_us=0; uint32_t seq=0,elapsed_us=0; int32_t code=0,http=0; uint8_t kind=0,source=Unknown,before=0,after=0,phase=0,attempt=0,ack=0,error_kind=0; };
    // meeting.19: same 24 slots (internal RAM, copied on stacks), but a 60 min meeting
    // overflowed them after ~20 min. The first FirstEvents stay; the rest is a ring of
    // the newest events. dropped_events counts overwritten middle events; totals below
    // cover every attempt of the session.
    static constexpr size_t MaxEvents=24, FirstEvents=8, RingEvents=MaxEvents-FirstEvents;
    char session[33]{};
    Event events[MaxEvents]{};
    uint32_t event_count=0,dropped_events=0;          // event_count: slots in use (<=MaxEvents)
    uint32_t attempts_failed=0,attempts_recovered=0;  // whole session, never capped by slots
    uint8_t max_attempt=0;
    // Logical order: first events, then the ring oldest -> newest.
    const Event& EventAt(uint32_t i) const {
        if(i<FirstEvents||!dropped_events)return events[i];
        return events[FirstEvents+(dropped_events+(i-FirstEvents))%RingEvents];
    }
    // Event number in the whole session (gaps show where the ring overwrote).
    uint32_t EventNumber(uint32_t i) const { return i<FirstEvents?i:i+dropped_events; }
    void Reset(const char* id) {
        *this=MeetingDiagnostics{};
        std::strncpy(session,id,32);session[32]=0;
    }
    void Add(EventKind kind,Source source,uint64_t now,uint8_t before,uint8_t after) {
        Event* slot=nullptr;
        if(event_count<MaxEvents)slot=&events[event_count++];
        else if(dropped_events!=UINT32_MAX)slot=&events[FirstEvents+(dropped_events++)%RingEvents];
        if(slot) {
            auto& e=*slot;e=Event{};e.time_us=now;e.seq=current.seq;e.kind=kind;e.source=source;e.before=before;e.after=after;
            e.phase=current.request.phase;e.attempt=current.request.attempt;e.ack=current.request.ack;e.error_kind=current.request.error_kind;
            e.code=current.request.code;e.http=current.request.http;
            uint64_t elapsed=current.request.in_flight?now-current.request.started_us:current.request.elapsed_us;
            e.elapsed_us=elapsed>UINT32_MAX?UINT32_MAX:uint32_t(elapsed);
        }
    }
    bool Fatal(Reason reason,uint64_t now) {
        if(first.present)return false;
        timing.first=timing.current.Snapshot(now,current.queue_frames);
        first.present=true;first.reason=reason;first.time_us=now;first.context=current;
        if(first.context.request.in_flight)first.context.request.elapsed_us=now-first.context.request.started_us;
        Add(FatalLatched,Unknown,now,current.state,current.state);return true;
    }
    void RequestDone(bool ok,uint64_t now) {
        current.request.elapsed_us=now-current.request.started_us;
        current.request.in_flight=false;
        if(current.request.attempt>max_attempt)max_attempt=current.request.attempt;
        if(!ok){if(attempts_failed!=UINT32_MAX)++attempts_failed;Add(HttpAttemptFailed,Unknown,now,current.state,current.state);}
        else if(current.request.attempt>1){if(attempts_recovered!=UINT32_MAX)++attempts_recovered;Add(HttpRetryRecovered,Unknown,now,current.state,current.state);}
        // Failure of one attempt, including cleanup/status, never latches a fatal by itself.
    }
};
static_assert(sizeof(MeetingDiagnostics)<4096,"Diagnostic snapshot including timing must stay below 4 KiB");
