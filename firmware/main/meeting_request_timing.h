#pragma once
#include <cstdint>
#include <cstddef>
#include <cstdio>

// Same nano-safe uint64 decimal conversion verified on meeting.9 hardware.
inline const char* DiagnosticMicros(uint64_t value, char (&buffer)[21]) {
    char* cursor = buffer + sizeof(buffer) - 1;
    *cursor = '\0';
    do { *--cursor = char('0' + value % 10); value /= 10; } while (value);
    return cursor;
}
struct MeetingRequestTiming {
    enum Stage : uint8_t { Setup, Open, Write, Headers, Body, Cleanup, Ack, Done };
    uint64_t started_us=0, stage_started_us=0, ended_us=0, us[7]{};
    uint32_t seq=0, queue_before=0, queue_after=0, connects_before=0, connects_after=0;
    int32_t code=0, http=0;
    uint8_t phase=0, attempt=0, stage=Done, result=0; // 0 pending,1 HTTP fail,2 HTTP ok,3 ACK ok,4 ACK invalid
    void Begin(uint64_t now,uint32_t sequence,uint8_t p,uint8_t a,uint32_t queue) {
        *this={};stage=Setup;started_us=stage_started_us=now;seq=sequence;phase=p;attempt=a;queue_before=queue_after=queue;
    }
    void Step(Stage next,uint64_t now) {
        if(stage<Done)us[stage]+=now-stage_started_us;
        stage=next;stage_started_us=now;
    }
    MeetingRequestTiming Snapshot(uint64_t now,uint32_t queue) const {
        auto copy=*this;copy.queue_after=queue;
        if(copy.phase && copy.stage<Done)copy.us[copy.stage]+=now-copy.stage_started_us;
        // stage_started_us remains the real stage entry; do not resume this frozen copy.
        return copy;
    }
};
struct MeetingTimingHistory {
    static constexpr uint32_t Capacity=8;
    MeetingRequestTiming current, first, recent[Capacity];
    uint64_t total_us[7]{};
    uint32_t count=0, total=0, successful=0, slow_success=0, max_queue=0;
    void Finish(uint64_t now,uint32_t queue,uint8_t result,int32_t code,int32_t http) {
        current.Step(MeetingRequestTiming::Done,now);current.ended_us=now;
        current.queue_after=queue;current.result=result;current.code=code;current.http=http;
        recent[total%Capacity]=current;if(count<Capacity)++count;++total;
        for(unsigned i=0;i<7;++i)total_us[i]+=current.us[i];
        if(queue>max_queue)max_queue=queue;
        if(result==2||result==3){++successful;if(now-current.started_us>=100000)++slow_success;}
    }
    const MeetingRequestTiming& At(uint32_t chronological) const { return recent[(total-count+chronological)%Capacity]; }
};
// Bounded, allocation-free. Only nano-supported types reach snprintf.
inline bool FormatMeetingTiming(char* out,size_t capacity,const char* sid,const char* role,uint32_t index,const MeetingRequestTiming& t) {
    char n[10][21];
    int length=snprintf(out,capacity,
        "timing sid=%s role=%s n=%lu seq=%lu phase=%u attempt=%u stage=%u result=%u begin_us=%s stage_us=%s end_us=%s setup_us=%s open_us=%s write_us=%s headers_us=%s body_us=%s cleanup_us=%s ack_us=%s q_before=%lu q_after=%lu code=%ld http=%ld connects_before=%lu connects_after=%lu",
        sid,role,(unsigned long)index,(unsigned long)t.seq,unsigned(t.phase),unsigned(t.attempt),unsigned(t.stage),unsigned(t.result),
        DiagnosticMicros(t.started_us,n[0]),DiagnosticMicros(t.stage_started_us,n[1]),DiagnosticMicros(t.ended_us,n[2]),
        DiagnosticMicros(t.us[0],n[3]),DiagnosticMicros(t.us[1],n[4]),DiagnosticMicros(t.us[2],n[5]),DiagnosticMicros(t.us[3],n[6]),DiagnosticMicros(t.us[4],n[7]),DiagnosticMicros(t.us[5],n[8]),DiagnosticMicros(t.us[6],n[9]),
        (unsigned long)t.queue_before,(unsigned long)t.queue_after,(long)t.code,(long)t.http,(unsigned long)t.connects_before,(unsigned long)t.connects_after);
    return length>=0 && size_t(length)<capacity;
}
static_assert(sizeof(MeetingTimingHistory)<=1536,"Request timing RAM must stay bounded");
