#pragma once
#include "device_state.h"
#include <cstdint>
#include <string>

namespace MeetingVoiceControl {
enum class Action { Ignore, WaitForCommand, Stop, Delegate };
inline bool CanContinueWake(DeviceState state, bool direct_idle) {
    return state==kDeviceStateConnecting || (direct_idle && state==kDeviceStateIdle);
}
inline bool SuppressReply(bool meeting, int64_t deadline, bool pending) {
    return meeting && (deadline==0 || pending);
}
inline const char* Name(Action a) {
    return a==Action::Stop?"stop":a==Action::WaitForCommand?"wake_only":a==Action::Delegate?"delegate":"ignore";
}

// meeting.23: tolerant whole-utterance matching (never substring matching).
// Known lead-ins (wake word and its common ASR variants, polite openers) and trailing
// particles are stripped; what remains must be exactly a stop phrase. Anything else,
// including quotations ("他说结束会议记录") and negations ("先别结束会议记录"), is not
// a stop. A wake word alone (or with only particles) waits for the command silently.
namespace detail {
inline bool StartsWith(const std::string& s,const char* p){const std::string q(p);return s.size()>=q.size()&&s.compare(0,q.size(),q)==0;}
inline bool EndsWith(const std::string& s,const char* p){const std::string q(p);return s.size()>=q.size()&&s.compare(s.size()-q.size(),q.size(),q)==0;}
inline bool StripAnyPrefix(std::string& s,std::initializer_list<const char*> list){
    for(const char* p:list)if(StartsWith(s,p)){s.erase(0,std::string(p).size());return true;}
    return false;
}
inline bool StripAnySuffix(std::string& s,std::initializer_list<const char*> list){
    for(const char* p:list)if(EndsWith(s,p)){s.erase(s.size()-std::string(p).size());return true;}
    return false;
}
inline std::string Normalize(const std::string& text){
    std::string n=text;
    for(const std::string token:{" ","\t","\n","\r","　",",",".","!","?",":",";","，","。","！","？","：","；","、","~","～","…","\"","“","”","'"}){
        size_t at;while((at=n.find(token))!=std::string::npos)n.erase(at,token.size());
    }
    return n;
}
// Longest first so "你好小智" is not left as "你好" + "小智".
#define MVC_WAKE "你好小智","你好小志","你好晓智","你好小知","你好小芝","您好小智","嗨小智","哈喽小智","小智同学","小智","小志","晓智"
#define MVC_OPENER "请帮我","麻烦你","麻烦","帮我","请你","请","好了","好的","好","那就","那","现在","可以","我们","我要","我想"
#define MVC_TAIL "一下吧","一下","吧","了","啊","呀","呢","哦","嘛","哈","啦"
}
inline Action Classify(const std::string& text, bool recording, int64_t now, int64_t deadline) {
    using namespace detail;
    if (!recording || deadline==0 || now>=deadline) return Action::Ignore;
    std::string n=Normalize(text);
    bool woke=false;
    for(int i=0;i<4;++i){ // wake word possibly repeated, openers in any order
        bool changed=StripAnyPrefix(n,{MVC_WAKE});woke|=changed;
        changed|=StripAnyPrefix(n,{MVC_OPENER});
        if(!changed)break;
    }
    while(StripAnySuffix(n,{MVC_TAIL})){}
    if(n.empty())return woke?Action::WaitForCommand:Action::Ignore;
    // Explicit negation guard (also covered by whole-utterance matching below).
    for(const char* neg:{"不","别","没","未","勿","莫"})if(StartsWith(n,neg))return Action::Delegate;
    for(const char* stop:{"结束会议记录","停止会议记录","结束记录会议","停止记录会议","结束会议纪要","停止会议纪要",
                          "结束会议录音","停止会议录音","结束录音","停止录音","结束记录","停止记录","结束会议","关闭会议记录",
                          "结束一下会议记录","停止一下会议记录","结束一下录音","停止一下录音","结束一下记录",
                          "会议记录结束","会议记录停止","会议结束","记录结束","录音结束"})
        if(n==stop)return Action::Stop;
    return Action::Delegate; // other requests stay with MCP/the assistant
}
#undef MVC_WAKE
#undef MVC_OPENER
#undef MVC_TAIL
}
