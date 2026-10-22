#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

// meeting.22: find the receiver on whatever LAN the board is on (home MacBook, office
// Mac mini) instead of a fixed IP. Portable parsing/checks; used by MeetingRecorder
// and native regression. Protocol (receiver r6, device_receiver.py):
//   device -> UDP broadcast :Port   "XZMEET/1 Q <32 lowercase hex nonce>"
//   receiver -> device              "XZMEET/1 A <http port> <64 hex HMAC-SHA256(token,
//                                    'XZMEET/1|<nonce>|<port>')>"
// The device accepts an answer only from a private-LAN IPv4 sender and only if the HMAC
// matches, so a LAN impostor cannot attract audio and the token never travels.
namespace MeetingDiscovery {
constexpr uint16_t Port=8768;
constexpr int Attempts=3;
constexpr int64_t AttemptWindowUs=500000;   // per broadcast; <=1.5 s before start
constexpr size_t NonceHex=32, MacHex=64;

inline bool IsLowerHex(const char* s,size_t n){
    for(size_t i=0;i<n;++i){char c=s[i];if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return false;}
    return true;
}
// ip in host byte order
inline bool IsLan(uint32_t ip){
    return (ip>>24)==10 || (ip>>20)==((172u<<4)|1u) || (ip>>16)==((192u<<8)|168u);
}
// Exact "XZMEET/1 A <port> <64 hex>" (no trailing bytes except one optional newline).
inline bool ParseAnswer(const char* data,size_t n,uint16_t* port,char mac[MacHex+1]){
    static const char prefix[]="XZMEET/1 A ";
    const size_t p=sizeof(prefix)-1;
    if(n&&data[n-1]=='\n')--n;
    if(n<p+1+1+MacHex||std::memcmp(data,prefix,p)!=0)return false;
    size_t i=p;uint32_t value=0;size_t digits=0;
    while(i<n&&data[i]>='0'&&data[i]<='9'){value=value*10+uint32_t(data[i]-'0');if(++digits>5)return false;++i;}
    if(!digits||value==0||value>65535||i>=n||data[i]!=' ')return false;
    ++i;
    if(n-i!=MacHex||!IsLowerHex(data+i,MacHex))return false;
    std::memcpy(mac,data+i,MacHex);mac[MacHex]=0;*port=uint16_t(value);
    return true;
}
inline bool ConstantTimeEqual(const char* a,const char* b,size_t n){
    unsigned char d=0;for(size_t i=0;i<n;++i)d|=static_cast<unsigned char>(a[i]^b[i]);return d==0;
}
}
