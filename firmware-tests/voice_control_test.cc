// meeting.23: meeting voice command classification (native).
#include "../firmware/main/meeting_voice_control.h"
#include <cassert>
#include <cstdio>
using namespace MeetingVoiceControl;
static Action C(const char* t){return Classify(t,true,0,15000000);}
int main(){
    // Wake word alone, ASR variants and particles: wait silently (no assistant reply).
    for(const char* t:{"你好小智","你好，小智。","你好小志","你好晓智！","小智","小智呀","你好小智啊","你好小智你好小智","嗨，小智？","你好小知。"})
        assert(C(t)==Action::WaitForCommand);
    // Stop, with or without wake word, openers, particles, synonyms.
    for(const char* t:{"结束会议记录","你好小智结束会议记录","你好小智，结束会议记录。","结束会议记录吧","好了，结束会议记录吧",
                       "请帮我结束会议记录","帮我结束一下会议记录","那就结束会议吧","停止录音","结束录音了","会议记录结束",
                       "你好小志，停止会议记录","小智，请结束一下录音。","我们结束会议吧","会议结束了"})
        assert(C(t)==Action::Stop);
    // Never stop: negations, quotations, questions, other requests.
    for(const char* t:{"不要结束会议记录","别结束会议记录","先别结束会议记录","不用停止录音","他说结束会议记录",
                       "结束会议记录了吗","什么时候结束会议记录","会议记录多久了","你好小智今天天气怎么样","结束会议记录和录音"})
        assert(C(t)==Action::Delegate);
    // Outside the command window or not recording: ignored entirely.
    assert(Classify("结束会议记录",true,15000000,15000000)==Action::Ignore);
    assert(Classify("结束会议记录",false,0,15000000)==Action::Ignore);
    assert(Classify("结束会议记录",true,0,0)==Action::Ignore);
    assert(C("。，")==Action::Ignore);
    assert(std::string(Name(Action::Stop))=="stop"&&std::string(Name(Action::WaitForCommand))=="wake_only");
    std::puts("PASS: wake-only variants wait silently; 15 stop phrasings stop; negations/quotations/questions delegate; window/recording gate");
}
