// meeting.19: first-8 + newest-16 event ring with whole-session attempt totals (native).
#include "../firmware/main/meeting_diagnostics.h"
#include <cassert>
#include <cstdio>
int main(){
    static MeetingDiagnostics d; d.Reset("0123456789abcdef0123456789abcdef");
    static_assert(MeetingDiagnostics::MaxEvents==24 && MeetingDiagnostics::FirstEvents==8,"same RAM as meeting.18");
    // Fewer than 24 events: unchanged behaviour.
    for(uint32_t i=0;i<10;++i){d.current.seq=i;d.Add(MeetingDiagnostics::StateChanged,MeetingDiagnostics::Unknown,i,0,0);}
    assert(d.event_count==10 && d.dropped_events==0);
    for(uint32_t i=0;i<10;++i)assert(d.EventAt(i).seq==i && d.EventNumber(i)==i);
    // 1,000 events (a long meeting): first 8 kept, last 16 are the newest, in order.
    d.Reset("0123456789abcdef0123456789abcdef");
    for(uint32_t i=0;i<1000;++i){d.current.seq=i;d.Add(MeetingDiagnostics::StateChanged,MeetingDiagnostics::Unknown,i,0,0);}
    assert(d.event_count==24 && d.dropped_events==976);
    for(uint32_t i=0;i<8;++i)assert(d.EventAt(i).seq==i && d.EventNumber(i)==i);
    for(uint32_t i=8;i<24;++i)assert(d.EventAt(i).seq==976+i && d.EventNumber(i)==976+i);
    assert(d.EventAt(23).seq==999);
    // Every count of overflow lands correctly (wraparound at each ring offset).
    for(uint32_t total=24;total<24+3*16;++total){
        d.Reset("x");for(uint32_t i=0;i<total;++i){d.current.seq=i;d.Add(MeetingDiagnostics::AutoStop,MeetingDiagnostics::Unknown,i,0,0);}
        for(uint32_t i=0;i<24;++i)assert(d.EventAt(i).seq==d.EventNumber(i) && d.EventNumber(i)<total);
        assert(d.EventAt(23).seq==total-1);
    }
    // Attempt totals are not capped by slots: 26 failures each recovered on attempt 2.
    d.Reset("x");
    for(int n=0;n<26;++n){
        d.current.request.attempt=1;d.RequestDone(false,1);
        d.current.request.attempt=2;d.RequestDone(true,2);
    }
    assert(d.attempts_failed==26 && d.attempts_recovered==26 && d.max_attempt==2 && d.event_count==24 && d.dropped_events==28);
    assert(d.EventAt(23).kind==MeetingDiagnostics::HttpRetryRecovered);
    std::puts("PASS: diagnostic events keep first 8 + newest 16 in order at every wrap offset; failed/recovered totals uncapped; same 24 slots");
}
