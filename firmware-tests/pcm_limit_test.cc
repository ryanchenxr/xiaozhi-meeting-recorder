// meeting.18 60-minute limit check against the real portable capture core (synthetic only).
#include "../firmware/main/meeting_pcm.h"
#include <cassert>
#include <cstdio>
#include <vector>
int main(){
    static_assert(MeetingPcm::MaxSeconds==3600 && MeetingPcm::MaxSamples==57600000u,"60-minute limit");
    static_assert(MeetingPcm::MaxSamples%960==0 && MeetingPcm::MaxSamples/960==60000u,"60,000 Opus packets");
    MeetingPcm p;uint64_t frames=0,samples=0;uint32_t partial=0;int16_t last=-1;bool ordered=true;
    auto emit=[&](const MeetingPcm::Frame& f){++frames;samples+=f.count;if(f.count!=160)++partial;
        for(int i=0;i<f.count;++i){if(f.samples[i]!=int16_t((last+1)&0x7fff))ordered=false;last=f.samples[i];}return true;};
    // Real resampler-like chunks: 10 ms stereo (320 int16) with a ramp on the mic channel.
    std::vector<int16_t> in(320);int16_t v=0;MeetingPcm::Result r=MeetingPcm::Continue;uint64_t chunks=0;
    while(r==MeetingPcm::Continue){
        for(int i=0;i<160;++i){in[2*i]=v;in[2*i+1]=-1;v=int16_t((v+1)&0x7fff);}
        r=p.Feed(in.data(),in.size(),2,emit);++chunks;
        if(p.samples==180u*16000||p.samples==600u*16000)assert(r==MeetingPcm::Continue); // old 3/10-minute points: keep going
    }
    assert(r==MeetingPcm::Limit && chunks==360000);
    assert(p.samples==MeetingPcm::MaxSamples && samples==MeetingPcm::MaxSamples);
    assert(frames==360000 && partial==0 && ordered);                         // exact boundary, no tail frame
    assert(p.Flush(emit) && frames==360000);                                 // Stop after limit: nothing extra
    assert(p.Feed(in.data(),in.size(),2,emit)==MeetingPcm::Limit && frames==360000);
    // Unaligned chunks crossing the boundary: Limit exactly at MaxSamples, remainder discarded.
    p.Reset();frames=samples=0;partial=0;last=-1;v=0;std::vector<int16_t> odd(2*173);
    do{for(int i=0;i<173;++i){odd[2*i]=v;odd[2*i+1]=-1;v=int16_t((v+1)&0x7fff);}r=p.Feed(odd.data(),odd.size(),2,emit);}while(r==MeetingPcm::Continue);
    assert(r==MeetingPcm::Limit && samples==MeetingPcm::MaxSamples && p.samples==MeetingPcm::MaxSamples && frames==360000 && partial==0 && ordered);
    std::puts("PASS: 3600 s limit: no stop at 180/600 s, Limit at exactly 57,600,000 samples (60,000 Opus packets), 360,000 full frames, no extra tail frame, unaligned boundary exact");
}
