#pragma once
#include <cstddef>
#include <cstdint>

// Portable capture core; used by the real AudioService tap and native regression.
class MeetingPcm {
public:
    struct Frame { uint16_t count=0; int16_t samples[160]; };
    enum Result { Continue, Limit, Overflow, Invalid };
    static constexpr uint32_t MaxSeconds=3600; // 60-minute meeting limit (meeting.18)
    static constexpr uint32_t MaxSamples=MaxSeconds*16000;
    uint32_t samples=0;
    void Reset(){samples=0;pending_.count=0;}
    template<class Emit> bool Flush(Emit emit){
        if(!pending_.count)return true;
        if(!emit(pending_))return false;
        pending_.count=0;return true;
    }
    template<class Emit> Result Feed(const int16_t* data,size_t size,int channels,Emit emit){
        if(channels!=2 || size%2)return Invalid;
        if(samples>=MaxSamples)return Limit;
        for(size_t i=0;i<size;i+=2){
            pending_.samples[pending_.count++]=data[i]; // microphone; data[i+1] is reference
            ++samples;
            if(pending_.count==160&&!Flush(emit))return Overflow;
            if(samples==MaxSamples)return Flush(emit)?Limit:Overflow;
        }
        return Continue;
    }
private:
    Frame pending_;
};
