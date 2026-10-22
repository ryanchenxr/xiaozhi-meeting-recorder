#include "../firmware/main/meeting_pcm.h"
#include <cassert>
#include <vector>
#include <cstdio>
int main(){
    MeetingPcm p;std::vector<int16_t> out;
    auto emit=[&](const MeetingPcm::Frame& f){out.insert(out.end(),f.samples,f.samples+f.count);return true;};
    int16_t pair[]={123,-999};
    for(int i=0;i<159;++i)assert(p.Feed(pair,2,2,emit)==MeetingPcm::Continue);
    assert(out.empty());assert(p.samples==159);
    assert(p.Flush(emit));assert(out.size()==159);
    for(auto sample:out)assert(sample==123); // never reference -999
    assert(p.Flush(emit));assert(out.size()==159); // stop flush idempotent
    assert(p.Feed(pair,1,2,emit)==MeetingPcm::Invalid);
    assert(p.Feed(pair,2,1,emit)==MeetingPcm::Invalid);
    p.Reset();out.clear();
    for(uint32_t i=0;i<MeetingPcm::MaxSamples;++i){
        auto r=p.Feed(pair,2,2,emit);
        assert(r==(i+1==MeetingPcm::MaxSamples?MeetingPcm::Limit:MeetingPcm::Continue));
    }
    assert(out.size()==MeetingPcm::MaxSamples);assert(p.Feed(pair,2,2,emit)==MeetingPcm::Limit);
    assert(out.size()==MeetingPcm::MaxSamples);
    p.Reset();std::vector<int16_t> input(320,0);
    assert(p.Feed(input.data(),input.size(),2,[](const auto&){return false;})==MeetingPcm::Overflow);
    // Variable resampler chunks, including zero, do not drop or reorder microphone data.
    p.Reset();out.clear();std::vector<int16_t> expected;
    for(int n=0;n<500;++n){int count=n%173;input.resize(count*2);
        for(int i=0;i<count;++i){input[2*i]=n;input[2*i+1]=-1;expected.push_back(n);}
        assert(p.Feed(input.data(),input.size(),2,emit)==MeetingPcm::Continue);
    }
    assert(p.Flush(emit));assert(out==expected);
    puts("PASS: actual capture core channel mapping, partial flush, reset, invalid input, overflow, exact MaxSamples limit (now 3600 s), variable-frame continuity (synthetic only)");
}
