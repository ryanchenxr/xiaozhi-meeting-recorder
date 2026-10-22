#pragma once
#include <cstdint>
// Application-owned, fixed log history. No capture-thread writes or allocations.
struct MeetingPowerTrace {
    struct Entry { uint64_t us=0; uint32_t n=0; int32_t actual=-1,query_error=0; uint8_t reason=0,demands=0,meeting=0,state=0,requested=0; };
    static constexpr unsigned Capacity=16;
    Entry entries[Capacity]{};
    uint32_t total=0;
    void Add(Entry e){e.n=total;entries[total%Capacity]=e;++total;}
};
