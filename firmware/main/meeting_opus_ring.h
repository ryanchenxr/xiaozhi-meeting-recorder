#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

// Portable byte ring of framed Opus packets: [u16 little-endian length][payload].
// One producer (encoder task) and one consumer (upload worker); the caller holds
// the lock around every call. Used by MeetingRecorder and native regression.
class MeetingOpusRing {
public:
    static constexpr uint16_t MaxPacket=1275; // RFC 6716 maximum Opus packet
    void Attach(uint8_t* storage,size_t capacity){data_=storage;capacity_=capacity;Reset();}
    void Reset(){head_=tail_=used_=0;packets_=0;}
    bool Ready() const {return data_!=nullptr && capacity_>2u+MaxPacket;}
    size_t Used() const {return used_;}
    size_t Capacity() const {return capacity_;}
    uint32_t Packets() const {return packets_;}
    // False when the packet is invalid or does not fit; nothing is written then.
    bool Push(const uint8_t* packet,size_t size){
        if(!Ready() || size==0 || size>MaxPacket || used_+2+size>capacity_)return false;
        uint8_t header[2]={uint8_t(size&0xff),uint8_t(size>>8)};
        Write(header,2);Write(packet,size);++packets_;return true;
    }
    // Moves whole framed packets (headers included) into out, at most max bytes.
    // Returns bytes moved; *count receives the number of packets.
    size_t Take(uint8_t* out,size_t max,uint32_t* count){
        size_t moved=0;uint32_t n=0;
        while(packets_){
            uint8_t header[2];Peek(header,2);
            size_t record=2+size_t(header[0]|(header[1]<<8));
            if(moved+record>max)break;
            Read(out+moved,record);moved+=record;++n;--packets_;
        }
        if(count)*count=n;
        return moved;
    }
private:
    uint8_t* data_=nullptr;size_t capacity_=0,head_=0,tail_=0,used_=0;uint32_t packets_=0;
    void Write(const uint8_t* src,size_t n){
        size_t first=n<capacity_-head_?n:capacity_-head_;
        memcpy(data_+head_,src,first);memcpy(data_,src+first,n-first);
        head_=(head_+n)%capacity_;used_+=n;
    }
    void Peek(uint8_t* dst,size_t n) const {
        size_t first=n<capacity_-tail_?n:capacity_-tail_;
        memcpy(dst,data_+tail_,first);memcpy(dst+first,data_,n-first);
    }
    void Read(uint8_t* dst,size_t n){Peek(dst,n);tail_=(tail_+n)%capacity_;used_-=n;}
};
