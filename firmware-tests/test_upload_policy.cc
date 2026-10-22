// Native check of the meeting.16 upload budget: clang++ -std=c++17 -I../source/main
#include "meeting_upload_policy.h"
#include <cassert>
#include <cstdio>
using namespace MeetingUpload;
int main(){
    assert(MaxChunkBytes==64000 && OpusFrameSamples==960 && OpusFramesPerPacket==6);
    assert(RetryDelayMs(1)==100 && RetryDelayMs(2)==250 && RetryDelayMs(5)==500 && RetryDelayMs(9)==1000);
    // meeting.20: 600 s chunk window (was 240 s).
    assert(ShouldRetry(0,0,1) && ShouldRetry(0,598800000,6) && !ShouldRetry(0,599500000,6) && ShouldRetry(0,300000000,60));
    // Field 2026-10-19: a ~30 s stall killed the session at the old 30 s window.
    assert(ShouldRetry(0,35000000,8) && ShouldRetry(0,120000000,40));
    assert(!ShouldRetry(0,0,250));
    // Field 2026-10-19: link fell to ~8.5 KB/s. Opus nominal 4 KB/s leaves ~2x margin.
    assert(8500.0/OpusNominalBytesPerSecond>2.0);
    // Ring: at the nominal rate it holds > 13 minutes of audio (meeting.20: 3 MiB).
    assert(OpusRingBytes==3u*1024*1024 && OpusRingBytes/OpusNominalBytesPerSecond>780);
    // meeting.19: start retried for 15 s (field: first connect after idle took >1.2 s).
    assert(ShouldRetryStart(0,1216154,1) && ShouldRetryStart(0,13900000,10) && !ShouldRetryStart(0,14000000,10));
    assert(!ShouldRetryStart(0,0,20));
    { int64_t t=0;uint32_t n=0;while(ShouldRetryStart(0,t,++n)){t+=int64_t(RetryDelayMs(n))*1000+1216154;} assert(n>=8); } // >=8 attempts at 1.2 s each
    // Field 2026-10-19: finish took 1.20 s on a 1.2 s timeout; control requests get 5 s.
    assert(ChunkTimeoutMs==3000 && ControlTimeoutMs==5000);   // meeting.20: chunk write 3 s
    // Adaptive cap for new chunks: 16 KB -> halve on failure to 4 KB floor -> double back.
    assert(NextChunkCap(16000,true)==8000 && NextChunkCap(8000,true)==4000 && NextChunkCap(4000,true)==4000);
    assert(NextChunkCap(4000,false)==8000 && NextChunkCap(8000,false)==16000 && NextChunkCap(16000,false)==16000);
    { size_t c=ChunkCapMaxBytes;for(int i=0;i<50;++i)c=NextChunkCap(c,true);assert(c==ChunkCapMinBytes);for(int i=0;i<3;++i)c=NextChunkCap(c,false);assert(c==ChunkCapMaxBytes); }
    assert(ChunkCapMinBytes>=2+1275 && ChunkCapMaxBytes<=MaxChunkBytes);
    std::puts("upload policy (opus, 600 s chunk window, 3 MiB ring, 16->4 KB adaptive cap, 15 s start window, 3/5 s timeouts): OK");
}
