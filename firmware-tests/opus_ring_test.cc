// Native check of the portable Opus packet ring (synthetic packets).
#include "../firmware/main/meeting_opus_ring.h"
#include "../firmware/main/meeting_upload_policy.h"
#include "../firmware/main/meeting_pcm.h"
#include <cassert>
#include <cstdio>
#include <vector>
int main(){
    std::vector<uint8_t> storage(4096);MeetingOpusRing r;r.Attach(storage.data(),storage.size());
    assert(r.Ready()&&r.Used()==0&&r.Packets()==0);
    uint8_t p[MeetingOpusRing::MaxPacket+1];for(size_t i=0;i<sizeof(p);++i)p[i]=uint8_t(i*7+1);
    assert(!r.Push(p,0)&&!r.Push(p,MeetingOpusRing::MaxPacket+1)&&r.Used()==0);   // invalid sizes rejected
    // Fill/drain repeatedly with varying sizes so records wrap at every offset.
    std::vector<uint8_t> out(64000);uint32_t pushed=0,taken=0,seed=1;std::vector<size_t> sizes;
    for(int round=0;round<5000;++round){
        size_t n=1+(seed=seed*1103515245+12345)%300;
        if(r.Push(p+(round%50),n)){sizes.push_back(n);++pushed;}
        else {                                                                       // full: never partial
            uint32_t count=0;size_t got=r.Take(out.data(),out.size(),&count);assert(count>0&&got>0);
            size_t off=0;for(uint32_t k=0;k<count;++k){size_t len=out[off]|(out[off+1]<<8);assert(len==sizes[taken+k]);off+=2+len;}
            assert(off==got);taken+=count;
        }
    }
    uint32_t count=0;r.Take(out.data(),out.size(),&count);taken+=count;assert(taken==pushed&&r.Used()==0&&r.Packets()==0);
    // Take honours max: never splits a record.
    r.Reset();for(int i=0;i<10;++i)assert(r.Push(p,100));
    size_t got=r.Take(out.data(),250,&count);assert(got==204&&count==2&&r.Packets()==8);
    got=r.Take(out.data(),1,&count);assert(got==0&&count==0&&r.Packets()==8);
    // Budget: 64,000 B chunk holds >=50 nominal packets; 3600 s = 60,000 packets.
    assert(MeetingUpload::MaxChunkBytes/(2+MeetingUpload::OpusNominalBytesPerSecond*60/1000)>=50);
    assert(MeetingPcm::MaxSamples/MeetingUpload::OpusFrameSamples==60000);
    std::puts("PASS: opus ring wraparound at all offsets, no partial records, invalid sizes rejected, Take max respected");
}
