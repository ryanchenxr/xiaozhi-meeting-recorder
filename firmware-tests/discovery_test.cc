// meeting.22: receiver discovery answer parsing and LAN filter (native).
#include "../firmware/main/meeting_discovery.h"
#include <cassert>
#include <cstdio>
#include <string>
using namespace MeetingDiscovery;
static uint32_t ip(unsigned a,unsigned b,unsigned c,unsigned d){return (a<<24)|(b<<16)|(c<<8)|d;}
int main(){
    const std::string mac(64,'a');char out[65];uint16_t port=0;
    std::string ok="XZMEET/1 A 8767 "+mac;
    assert(ParseAnswer(ok.data(),ok.size(),&port,out)&&port==8767&&mac==out);
    std::string nl=ok+"\n";assert(ParseAnswer(nl.data(),nl.size(),&port,out));
    for(std::string bad:{std::string(""),"XZMEET/1 A 8767 "+std::string(63,'a'),"XZMEET/1 A 8767 "+std::string(65,'a'),
            "XZMEET/1 A 8767 "+std::string(64,'A'),"XZMEET/1 A 0 "+mac,"XZMEET/1 A 65536 "+mac,"XZMEET/1 A 123456 "+mac,
            "XZMEET/1 A  8767 "+mac,"XZMEET/1 Q 8767 "+mac,"XZMEET/2 A 8767 "+mac,"XZMEET/1 A 8767x"+mac,ok+" "}){
        assert(!ParseAnswer(bad.data(),bad.size(),&port,out));
    }
    assert(IsLan(ip(192,168,1,17))&&IsLan(ip(10,0,0,1))&&IsLan(ip(172,16,0,1))&&IsLan(ip(172,31,255,255)));
    assert(!IsLan(ip(172,32,0,1))&&!IsLan(ip(198,18,1,135))&&!IsLan(ip(8,8,8,8))&&!IsLan(ip(127,0,0,1))&&!IsLan(ip(192,169,0,1)));
    assert(ConstantTimeEqual("abc","abc",3)&&!ConstantTimeEqual("abc","abd",3));
    std::puts("PASS: discovery answer parsing (exact format, port range, lowercase 64-hex), LAN filter, constant-time compare");
}
