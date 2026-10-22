#include "meeting_recorder.h"
#include "meeting_queue.h"
#include "meeting_upload_policy.h"
#include "application.h"
#include "board.h"
#include "display.h"
#include "lvgl_theme.h"
#include <cstring>
#include <memory>
#include <new>
#include "audio_service.h"
#include "../meeting_config.local.h"
#include <esp_http_client.h>
#include <lwip/sockets.h>
#include <mbedtls/md.h>
#include <unistd.h>
#include "meeting_discovery.h"
#include <esp_log.h>
#include <esp_heap_caps.h>
#if CONFIG_HEAP_TASK_TRACKING
#include <esp_heap_task_info.h>
#endif
#include <freertos/idf_additions.h>
#include "esp_opus_enc.h"
#include <esp_random.h>
#include <esp_timer.h>
#include <mbedtls/sha256.h>
#include <cJSON.h>
#include <lvgl.h>
#include "../managed_components/espressif2022__esp_emote_gfx/src/lib/qrcode/qrcodegen.h"
#include <algorithm>

MeetingRecorder& MeetingRecorder::Get() { static MeetingRecorder recorder; return recorder; }
void MeetingRecorder::UploadLoop() {
    for(;;){ulTaskNotifyTake(pdTRUE,portMAX_DELAY);Run();} // one session per notification
}
bool MeetingRecorder::EnsureUploadTaskLocked() {
    if(upload_task_)return true;
    if(xTaskCreate([](void* p){static_cast<MeetingRecorder*>(p)->UploadLoop();},"meeting_upload",8192,this,3,&upload_task_)!=pdPASS){upload_task_=nullptr;return false;}
    return true;
}
void MeetingRecorder::PrepareUploadTask() {
    std::lock_guard<std::mutex> lock(mutex_);
    if(!EnsureUploadTaskLocked())ESP_LOGW("Meeting","upload task not reserved at boot; Start will retry");
}
bool MeetingRecorder::Protected() const {
    auto s=state_.load();return s==Starting||s==RecordingState||s==Stopping;
}
bool MeetingRecorder::BlockOperation() {
    if (!Protected()) return false;
    Board::GetInstance().GetDisplay()->ShowNotification("会议记录中，请先结束记录");return true;
}
std::string MeetingRecorder::Status() {
    diagnostic_dump_requested_=true;
    std::lock_guard<std::mutex> lock(mutex_);
    static const char* names[]={"idle","starting","recording","stopping","processing","complete","saved_processing_not_authorized","error_incomplete_or_unavailable"};
    return std::string("{\"state\":\"")+names[state_.load()]+"\",\"session\":\""+session_+"\",\"seconds\":"+std::to_string(samples_/16000)+"}";
}
std::string MeetingRecorder::Start(MeetingDiagnostics::Source source) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (worker_running_ || Protected()) { diagnostic_.Add(MeetingDiagnostics::StartReused,source,esp_timer_get_time(),state_,state_); return "会议会话已存在："+session_+"，不重复开始。"; }
        if (!queue_) queue_=CreateMeetingQueue(queue_control_); // fixed ~15 s, see meeting_upload_policy.h
        if (queue_ && !send_buffer_) send_buffer_=static_cast<uint8_t*>(heap_caps_malloc(MeetingUpload::MaxChunkBytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        if (!queue_ || !send_buffer_ || !EnsureEncoderLocked()) { diagnostic_.Add(MeetingDiagnostics::StartAllocationFailed,source,esp_timer_get_time(),state_,state_);diagnostic_dump_requested_=true;return "会议缓冲分配失败，未开始录音"; }
        // Remove the previous result before accepting a new session.
        {std::lock_guard<std::mutex> ui_lock(ui_mutex_);
            DisplayLockGuard guard(Board::GetInstance().GetDisplay());
            if(qr_){lv_obj_delete(static_cast<lv_obj_t*>(qr_));qr_=nullptr;}
            if(panel_)lv_obj_add_flag(static_cast<lv_obj_t*>(panel_),LV_OBJ_FLAG_HIDDEN);
        }
        xQueueReset(queue_);
        // New generation after the reset: the encoder drops anything older.
        {std::lock_guard<std::mutex> ring_lock(ring_mutex_);ring_.Reset();encoded_samples_=0;++generation_;}
        pcm_.Reset();dismissed_=false;samples_=0;url_.clear();session_.clear();
        uint8_t id[16];esp_fill_random(id,sizeof(id));char hex[3];
        for(auto c:id){snprintf(hex,sizeof(hex),"%02x",c);session_+=hex;}
        previous_diagnostic_=diagnostic_;diagnostic_.Reset(session_.c_str());
        diagnostic_.Add(MeetingDiagnostics::StartAccepted,source,esp_timer_get_time(),state_,Starting);
        state_=Starting;worker_running_=true;performance_required_=true;
        Application::GetInstance().UpdatePowerSave(Application::PowerReason::MeetingBegin);
        if(EnsureUploadTaskLocked())xTaskNotifyGive(upload_task_);
        else{FatalLocked(MeetingDiagnostics::TaskCreate);state_=Error;worker_running_=false;performance_required_=false;Application::GetInstance().UpdatePowerSave(Application::PowerReason::MeetingTaskFailed);return "发送任务启动失败";}
    }
    return "正在检查会议接收端；屏幕显示正在记录后才开始。会议中需再次唤醒后说结束会议记录，也可单击BOOT停止。";
}
std::string MeetingRecorder::Stop(MeetingDiagnostics::Source source) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto before=state_.load();
    diagnostic_.current.stop_us=esp_timer_get_time();diagnostic_.current.stop_source=source;++diagnostic_.current.stop_requests;
    if(state_==RecordingState) { if(FlushFrame()&&PushEndOfStream())state_=Stopping; }
    else if(state_==Starting)state_=Stopping;
    diagnostic_.Add(MeetingDiagnostics::StopRequested,source,esp_timer_get_time(),before,state_);
    return "已请求结束会议；请查看屏幕确认完整性及处理状态，不重复提交。";
}
void MeetingRecorder::InputFailed(MeetingDiagnostics::Reason reason,int code) {
    std::lock_guard<std::mutex> lock(mutex_);
    if(state_==RecordingState) { diagnostic_.current.capture_code=code;FatalLocked(reason);state_=Error; }
}
bool MeetingRecorder::FlushFrame() {
    bool ok=pcm_.Flush([this](const Frame& frame){if(xQueueSend(queue_,&frame,0)!=pdTRUE)return false;diagnostic_.current.enqueued+=frame.count;return true;});
    if(!ok) {FatalLocked(MeetingDiagnostics::TailFlush);state_=Error;}
    return ok;
}
bool MeetingRecorder::PushEndOfStream() {
    // Caller holds mutex_. A zero-count frame never comes from capture: it tells the
    // encoder to flush its partial 60 ms packet and publish end-of-stream.
    Frame end;end.count=0;
    if(xQueueSend(queue_,&end,0)==pdTRUE)return true;
    FatalLocked(MeetingDiagnostics::TailFlush);state_=Error;return false;
}
bool MeetingRecorder::EnsureEncoderLocked() {
    // All three are created once and kept for the device lifetime (fixed PSRAM).
    if(!ring_.Ready()){
        auto* storage=static_cast<uint8_t*>(heap_caps_malloc(MeetingUpload::OpusRingBytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        if(!storage){ESP_LOGE("Meeting","Opus ring allocation failed");return false;}
        std::lock_guard<std::mutex> ring_lock(ring_mutex_);ring_.Attach(storage,MeetingUpload::OpusRingBytes);
    }
    if(!opus_encoder_){
        esp_opus_enc_config_t cfg={};
        cfg.sample_rate=ESP_AUDIO_SAMPLE_RATE_16K;cfg.channel=ESP_AUDIO_MONO;cfg.bits_per_sample=ESP_AUDIO_BIT16;
        cfg.bitrate=MeetingUpload::OpusBitrate;cfg.frame_duration=ESP_OPUS_ENC_FRAME_DURATION_60_MS;
        cfg.application_mode=ESP_OPUS_ENC_APPLICATION_VOIP;cfg.complexity=0;
        cfg.enable_fec=false;cfg.enable_dtx=false;cfg.enable_vbr=true; // no DTX: one packet per 60 ms
        void* encoder=nullptr;
        if(esp_opus_enc_open(&cfg,sizeof(cfg),&encoder)!=ESP_AUDIO_ERR_OK || !encoder){ESP_LOGE("Meeting","Opus encoder open failed");return false;}
        int in_size=0,out_size=0;esp_opus_enc_get_frame_size(encoder,&in_size,&out_size);
        if(in_size!=int(MeetingUpload::OpusFrameSamples*2) || out_size<=0){esp_opus_enc_close(encoder);ESP_LOGE("Meeting","Opus frame size %d/%d",in_size,out_size);return false;}
        opus_encoder_=encoder;
    }
    if(encoder_task_ && encoder_parked_.load() && eTaskGetState(encoder_task_)==eSuspended){
        vTaskDeleteWithCaps(encoder_task_);encoder_task_=nullptr; // a late park after a StopEncoder timeout
    }
    // An exit was requested but the old task has not parked yet: it would park in the
    // middle of this session. Refuse this Start explicitly; a later one succeeds.
    if(encoder_task_ && encoder_exit_requested_.load()){ESP_LOGW("Meeting","previous encoder still exiting");return false;}
    if(!encoder_task_){
        encoder_parked_=false;encoder_exit_requested_=false;
        // Stack in PSRAM: keeps it out of the tight internal heap. The task never
        // touches flash; it lives only during a session (see StopEncoder). 24 KB
        // overflowed on the device (VOIP mode runs SILK); 64 KB, high-water is dumped.
        if(xTaskCreatePinnedToCoreWithCaps([](void* p){static_cast<MeetingRecorder*>(p)->EncoderTask();},"meeting_opus",64*1024,this,4,&encoder_task_,tskNO_AFFINITY,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS){
            encoder_task_=nullptr;ESP_LOGE("Meeting","Opus task creation failed");return false;
        }
    }
    return true;
}
void MeetingRecorder::EncoderTask() {
    int16_t pcm[MeetingUpload::OpusFrameSamples];uint8_t packet[1500];
    uint32_t filled=0,local=generation_.load();
    esp_opus_enc_reset(opus_encoder_); // new task per session: no state from the previous one
    for(;;){
        Frame frame;
        if(xQueueReceive(queue_,&frame,portMAX_DELAY)!=pdTRUE)continue;
        if(frame.count==EncoderExitMarker){
            // Park holding no lock; StopEncoder (or the next Start) deletes this task.
            encoder_stack_min_free_=uxTaskGetStackHighWaterMark(nullptr);
            encoder_parked_=true;
            for(;;)vTaskSuspend(nullptr);
        }
        // Start resets queue_ before bumping generation_, and capture only begins
        // after the start request returns, so a new generation's first frame starts
        // a fresh packet. (meeting.16 v1 compared against a value read before the
        // blocking receive and dropped that first frame; DrainMismatch caught it.)
        // A stale frame slipping in would make encoded_samples_ exceed samples_ and
        // fail the same check: never silent.
        const uint32_t generation=generation_.load();
        if(generation!=local){local=generation;filled=0;esp_opus_enc_reset(opus_encoder_);}
        const bool end=frame.count==0;
        if(!end){std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.dequeued+=frame.count;}
        uint32_t offset=0;
        while(offset<frame.count || (end && filled)){
            uint32_t take=std::min<uint32_t>(frame.count-offset,MeetingUpload::OpusFrameSamples-filled);
            std::copy(frame.samples+offset,frame.samples+offset+take,pcm+filled);filled+=take;offset+=take;
            if(filled<MeetingUpload::OpusFrameSamples && !end)break;
            const uint32_t real=filled;
            std::fill(pcm+filled,pcm+MeetingUpload::OpusFrameSamples,int16_t(0)); // tail only
            esp_audio_enc_in_frame_t in={.buffer=reinterpret_cast<uint8_t*>(pcm),.len=uint32_t(sizeof(pcm))};
            esp_audio_enc_out_frame_t out={.buffer=packet,.len=uint32_t(sizeof(packet)),.encoded_bytes=0};
            filled=0;
            if(esp_opus_enc_process(opus_encoder_,&in,&out)!=ESP_AUDIO_ERR_OK || out.encoded_bytes==0){
                std::lock_guard<std::mutex> lock(mutex_);if(generation_.load()==local && Protected()){FatalLocked(MeetingDiagnostics::EncodeFailed);state_=Error;}
                break;
            }
            bool pushed,current;
            {std::lock_guard<std::mutex> ring_lock(ring_mutex_);current=generation_.load()==local;pushed=current && ring_.Push(packet,out.encoded_bytes);}
            if(current && !pushed){std::lock_guard<std::mutex> lock(mutex_);if(Protected()){FatalLocked(MeetingDiagnostics::RingFull);state_=Error;}break;}
            if(pushed)encoded_samples_+=real;
            if(end)break;
        }
        if(end && generation_.load()==local)encode_done_=local;
    }
}
void MeetingRecorder::StopEncoder() {
    // Upload worker only, after capture is disabled and finish/abort is done.
    TaskHandle_t task;{std::lock_guard<std::mutex> lock(mutex_);task=encoder_task_;}
    if(!task||!queue_)return;
    Frame exit{};exit.count=EncoderExitMarker;
    if(xQueueSend(queue_,&exit,pdMS_TO_TICKS(1000))!=pdTRUE){ESP_LOGW("Meeting","encoder exit not queued; kept");return;}
    encoder_exit_requested_=true;
    // Normally immediate; a backlog of queued frames (<=15 s of audio) takes a few s.
    for(int i=0;i<500&&!(encoder_parked_.load()&&eTaskGetState(task)==eSuspended);++i)vTaskDelay(pdMS_TO_TICKS(20));
    if(!(encoder_parked_.load()&&eTaskGetState(task)==eSuspended)){ESP_LOGW("Meeting","encoder exit timed out; next Start cleans up");return;}
    vTaskDeleteWithCaps(task); // releases its coprocessor (FPU/PIE) ownership
    std::lock_guard<std::mutex> lock(mutex_);if(encoder_task_==task){encoder_task_=nullptr;encoder_exit_requested_=false;}
}
void MeetingRecorder::Feed(const std::vector<int16_t>& pcm,int channels) {
    std::lock_guard<std::mutex> lock(mutex_);
    if(state_!=RecordingState)return;
    auto result=pcm_.Feed(pcm.data(),pcm.size(),channels,[this](const Frame& frame){if(xQueueSend(queue_,&frame,0)!=pdTRUE)return false;diagnostic_.current.enqueued+=frame.count;return true;});
    samples_=pcm_.samples;
    if(!pcm.empty())last_input_=esp_timer_get_time();
    if(result==MeetingPcm::Limit) {
        diagnostic_.current.stop_us=esp_timer_get_time();diagnostic_.current.stop_source=MeetingDiagnostics::Limit;++diagnostic_.current.stop_requests;
        diagnostic_.Add(MeetingDiagnostics::AutoStop,MeetingDiagnostics::Limit,esp_timer_get_time(),state_,Stopping);
        if(PushEndOfStream())state_=Stopping;
    } else if(result!=MeetingPcm::Continue) {FatalLocked(result==MeetingPcm::Invalid?MeetingDiagnostics::PcmInvalid:MeetingDiagnostics::QueueFull);state_=Error;}
}
void MeetingRecorder::ResetHttp() {
    if(http_client_){
        // meeting.21b: abort (RST) instead of a graceful close. A dropped connection on
        // a weak link still holds unacknowledged data; lwIP kept retransmitting it for
        // minutes (TCP_MAXRTX=12, RTO 1.5 s backoff) with its pcb and send buffers in
        // internal RAM, and consecutive failures stacked several of these (internal
        // low-water 383 B on 2026-10-19). The receiver dedups any retried chunk.
        const int fd=esp_http_client_get_socket(http_client_);
        if(fd>=0){struct linger abort_now={1,0};setsockopt(fd,SOL_SOCKET,SO_LINGER,&abort_now,sizeof(abort_now));}
        esp_http_client_cleanup(http_client_);http_client_=nullptr;
    }
}
esp_err_t MeetingRecorder::HttpEvent(esp_http_client_event_t* event) {
    if(event->event_id==HTTP_EVENT_ON_CONNECTED){
        auto* recorder=static_cast<MeetingRecorder*>(event->user_data);
        std::lock_guard<std::mutex> lock(recorder->mutex_);
        ++recorder->connection_count_;
        recorder->diagnostic_.timing.current.connects_after=recorder->connection_count_;
    }
    return ESP_OK;
}
bool MeetingRecorder::Request(const std::string& path,const void* data,size_t size,std::string& response,MeetingDiagnostics::Phase phase,uint8_t attempt,uint32_t seq) {
    MeetingDiagnostics::Request info;
    info.phase=phase;info.attempt=attempt;info.started_us=esp_timer_get_time();info.in_flight=true;
    // One mutex publication: seq, phase, attempt, HTTP state and timing identity.
    // Until this point current describes the preceding request (Done in a gap).
    {std::lock_guard<std::mutex> lock(mutex_);info.seq=seq;diagnostic_.current.seq=seq;if(phase==MeetingDiagnostics::ChunkHttp)info.expected_next=info.seq+1;diagnostic_.current.request=info;diagnostic_.timing.current.Begin(info.started_us,info.seq,phase,attempt,queue_?uxQueueMessagesWaiting(queue_):0);diagnostic_.timing.current.connects_before=diagnostic_.timing.current.connects_after=connection_count_;}
    auto stage=[&](MeetingRequestTiming::Stage next) {
        std::lock_guard<std::mutex> lock(mutex_);diagnostic_.timing.current.Step(next,esp_timer_get_time());
    };
    auto done=[&](bool ok) {
        std::lock_guard<std::mutex> lock(mutex_);
        diagnostic_.current.request=info;
        UpdateDiagnosticLocked();const auto now=esp_timer_get_time();diagnostic_.RequestDone(ok,now);
        if(ok && phase==MeetingDiagnostics::ChunkHttp)diagnostic_.timing.current.Step(MeetingRequestTiming::Ack,now);
        else diagnostic_.timing.Finish(now,diagnostic_.current.queue_frames,ok?2:1,info.code,info.http);
        return ok;
    };
    std::string url=(receiver_base_.empty()?std::string(MEETING_DEVICE_URL):receiver_base_)+path;
    std::string auth=std::string("Bearer ")+MEETING_DEVICE_TOKEN;
    if(!http_client_){
        esp_http_client_config_t cfg={};cfg.url=url.c_str();cfg.timeout_ms=MeetingUpload::ChunkTimeoutMs;cfg.disable_auto_redirect=true;
        cfg.event_handler=HttpEvent;cfg.user_data=this;
        http_client_=esp_http_client_init(&cfg);
        if(!http_client_){info.error_kind=1;info.code=ESP_ERR_NO_MEM;return done(false);}
    }
    auto client=http_client_;
    esp_http_client_set_timeout_ms(client,phase==MeetingDiagnostics::ChunkHttp?MeetingUpload::ChunkTimeoutMs:MeetingUpload::ControlTimeoutMs);
    // open() prepares the parser, request line and Content-Length in this locked IDF.
    // All variable headers are overwritten, including non-chunk attempt=0.
    info.code=esp_http_client_set_url(client,url.c_str());
    if(info.code==ESP_OK)info.code=esp_http_client_set_method(client,HTTP_METHOD_POST);
    if(info.code==ESP_OK)info.code=esp_http_client_set_header(client,"Authorization",auth.c_str());
    if(info.code==ESP_OK)info.code=esp_http_client_set_header(client,"Content-Type","application/octet-stream");
    if(info.code==ESP_OK)info.code=esp_http_client_set_header(client,"X-Meeting-Attempt",phase==MeetingDiagnostics::ChunkHttp?(attempt>=2?"2":"1"):"0");
    if(info.code!=ESP_OK){info.error_kind=1;stage(MeetingRequestTiming::Cleanup);ResetHttp();return done(false);}
    stage(MeetingRequestTiming::Open);
    info.code=esp_http_client_open(client,size);bool ok=info.code==ESP_OK;
    if(!ok)info.error_kind=2;
    if(ok)stage(MeetingRequestTiming::Write);
    size_t written=0;
    while(ok && written<size){int n=esp_http_client_write(client,static_cast<const char*>(data)+written,size-written);if(n<=0){info.error_kind=3;info.code=n;ok=false;break;}written+=n;info.payload_written=written;
        {std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.request.payload_written=written;if(phase==MeetingDiagnostics::ChunkHttp)diagnostic_.current.tx_pcm_bytes+=n;}
    }
    response.clear();
    if(ok){stage(MeetingRequestTiming::Headers);int64_t length=esp_http_client_fetch_headers(client);info.http=esp_http_client_get_status_code(client);ok=length>=0&&length<=2048&&info.http==200;
        if(!ok){info.error_kind=length<0?4:5;info.code=length<0?int32_t(length):0;}
        if(ok)stage(MeetingRequestTiming::Body);
        char buffer[256];while(ok && response.size()<static_cast<size_t>(length)){int n=esp_http_client_read(client,buffer,std::min<size_t>(sizeof(buffer),length-response.size()));if(n<=0){info.error_kind=6;info.code=n;ok=false;break;}response.append(buffer,n);}}
    if(ok && !esp_http_client_is_chunked_response(client) && !esp_http_client_is_complete_data_received(client)){info.error_kind=6;info.code=ESP_FAIL;ok=false;}
    stage(MeetingRequestTiming::Cleanup);
    // Failed/partial responses must not leave parser or cached bytes for a retry.
    // A non-persistent/unsupported chunked response is also released; never retry here.
    // Keep the old empty/chunked body outcome: ACK parsing rejects it without a new retry.
    if(!ok || esp_http_client_is_chunked_response(client) || !esp_http_client_is_persistent_connection(client))ResetHttp();
    return done(ok);
}
bool MeetingRecorder::ApplyResponse(const std::string& response) {
    auto root=cJSON_Parse(response.c_str());if(!root){std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.request.ack=2;return false;}
    auto id=cJSON_GetObjectItem(root,"session"),s=cJSON_GetObjectItem(root,"state"),u=cJSON_GetObjectItem(root,"url");
    bool ok=cJSON_IsString(id)&&session_==id->valuestring&&cJSON_IsString(s);
    {std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.request.ack=ok?1:2;diagnostic_.current.request.ack_session_match=cJSON_IsString(id)&&session_==id->valuestring;}
    if(ok){std::lock_guard<std::mutex> lock(mutex_);
        std::string value=s->valuestring;
        if(value=="complete"&&cJSON_IsString(u)&&strlen(u->valuestring)<=256&&std::string(u->valuestring).rfind("https://",0)==0){url_=u->valuestring;state_=Complete;}
        else if(value=="saved")state_=Saved;
        else if(value=="processing")state_=Processing;
        else {FatalLocked(diagnostic_.current.request.phase==MeetingDiagnostics::StatusHttpPhase?MeetingDiagnostics::StatusResponse:MeetingDiagnostics::FinishResponse);state_=Error;ok=false;}
    }
    cJSON_Delete(root);return ok;
}
void MeetingRecorder::ReleasePower() {
    if(performance_required_.exchange(false))Application::GetInstance().UpdatePowerSave(Application::PowerReason::MeetingEnd);
}
std::string MeetingRecorder::DiscoverReceiver() {
    const int s=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);if(s<0)return {};
    int on=1;setsockopt(s,SOL_SOCKET,SO_BROADCAST,&on,sizeof(on));
    struct timeval tv={0,200000};setsockopt(s,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));
    struct sockaddr_in local={};local.sin_family=AF_INET;local.sin_addr.s_addr=htonl(INADDR_ANY);
    if(bind(s,reinterpret_cast<struct sockaddr*>(&local),sizeof(local))!=0){close(s);return {};}
    uint8_t random[16];esp_fill_random(random,sizeof(random));
    char nonce[MeetingDiscovery::NonceHex+1];for(int i=0;i<16;++i)snprintf(nonce+2*i,3,"%02x",random[i]);
    char query[48];const int qn=snprintf(query,sizeof(query),"XZMEET/1 Q %s",nonce);
    struct sockaddr_in to={};to.sin_family=AF_INET;to.sin_port=htons(MeetingDiscovery::Port);to.sin_addr.s_addr=htonl(INADDR_BROADCAST);
    const mbedtls_md_info_t* sha=mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    std::string found;
    for(int attempt=0;attempt<MeetingDiscovery::Attempts&&found.empty();++attempt){
        sendto(s,query,qn,0,reinterpret_cast<struct sockaddr*>(&to),sizeof(to));
        const int64_t until=esp_timer_get_time()+MeetingDiscovery::AttemptWindowUs;
        while(found.empty()&&esp_timer_get_time()<until){
            char buf[160];struct sockaddr_in from={};socklen_t fl=sizeof(from);
            const int n=recvfrom(s,buf,sizeof(buf),0,reinterpret_cast<struct sockaddr*>(&from),&fl);
            if(n<=0)continue;
            uint16_t port=0;char mac[MeetingDiscovery::MacHex+1];
            const uint32_t ip=ntohl(from.sin_addr.s_addr);
            if(!MeetingDiscovery::ParseAnswer(buf,size_t(n),&port,mac)||!MeetingDiscovery::IsLan(ip))continue;
            char msg[64];const int mn=snprintf(msg,sizeof(msg),"XZMEET/1|%s|%u",nonce,unsigned(port));
            uint8_t digest[32];char expect[MeetingDiscovery::MacHex+1];
            if(!sha||mbedtls_md_hmac(sha,reinterpret_cast<const uint8_t*>(MEETING_DEVICE_TOKEN),std::strlen(MEETING_DEVICE_TOKEN),reinterpret_cast<const uint8_t*>(msg),mn,digest)!=0)continue;
            for(int i=0;i<32;++i)snprintf(expect+2*i,3,"%02x",digest[i]);
            if(!MeetingDiscovery::ConstantTimeEqual(expect,mac,MeetingDiscovery::MacHex))continue; // impostor or other key
            char base[40];snprintf(base,sizeof(base),"http://%u.%u.%u.%u:%u",unsigned(ip>>24),unsigned((ip>>16)&255),unsigned((ip>>8)&255),unsigned(ip&255),unsigned(port));
            found=base;
        }
    }
    close(s);return found;
}
void MeetingRecorder::ResolveReceiver() {
    const char* source="discovery";
    receiver_base_=DiscoverReceiver();
    if(!receiver_base_.empty())last_receiver_=receiver_base_;
    else if(!last_receiver_.empty()){receiver_base_=last_receiver_;source="cache";}
    else{receiver_base_=MEETING_DEVICE_URL;source="config";}
    ESP_LOGI("MeetingDiag","receiver source=%s url=%s",source,receiver_base_.c_str()); // LAN address only, no credential
}
void MeetingRecorder::Run() {
    struct Release {MeetingRecorder& owner;bool active=true;~Release(){if(active){owner.ResetHttp();owner.ReleasePower();}}
        void Now(){owner.ResetHttp();owner.ReleasePower();active=false;}} release{*this};
    ResetHttp();
    ResolveReceiver(); // every session: the board may have moved between LANs
    {std::lock_guard<std::mutex> lock(mutex_);connection_count_=0;}
    std::string response;uint32_t count=0,bytes=0,packets=0; // bytes: framed Opus stream
    const uint32_t generation=generation_.load(); // Start bumped it before creating this task
    mbedtls_sha256_context hash;mbedtls_sha256_init(&hash);mbedtls_sha256_starts(&hash,0);
    bool ok=Request("/v2/start/"+session_,nullptr,0,response,MeetingDiagnostics::StartHttp);
    {   // meeting.19: retry start (same session id; receiver start is idempotent).
        const int64_t first_started=esp_timer_get_time();uint32_t failed=0;
        while(!ok&&state_==Starting&&MeetingUpload::ShouldRetryStart(first_started,esp_timer_get_time(),++failed)){
            vTaskDelay(pdMS_TO_TICKS(MeetingUpload::RetryDelayMs(failed)));
            if(state_!=Starting)break;
            ok=Request("/v2/start/"+session_,nullptr,0,response,MeetingDiagnostics::StartHttp,uint8_t(std::min<uint32_t>(failed+1,255)));
        }
    }
    if(!ok)Fatal(MeetingDiagnostics::HttpFinal);
    if(ok){auto root=cJSON_Parse(response.c_str());auto id=cJSON_GetObjectItem(root,"session"),s=cJSON_GetObjectItem(root,"state");bool session_match=cJSON_IsString(id)&&session_==id->valuestring;ok=session_match&&cJSON_IsString(s)&&std::string(s->valuestring)=="recording";cJSON_Delete(root);
        std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.request.ack=ok?1:2;diagnostic_.current.request.ack_session_match=session_match;if(!ok)FatalLocked(MeetingDiagnostics::StartResponse);}
    if(ok){
        auto& audio=Application::GetInstance().GetAudioService();
        audio.EnableMeetingCapture(true);
        std::lock_guard<std::mutex> lock(mutex_);
        if(state_==Starting){diagnostic_.Add(MeetingDiagnostics::StateChanged,MeetingDiagnostics::Unknown,esp_timer_get_time(),Starting,RecordingState);state_=RecordingState;last_input_=esp_timer_get_time();}
        else if(state_==Stopping)PushEndOfStream(); // stopped while starting: empty stream, ends below
    }else state_=Error;
    size_t chunk_cap=MeetingUpload::ChunkCapMaxBytes;
    while(ok&&(state_==RecordingState||state_==Stopping)){
        uint8_t* block=send_buffer_;size_t length=0;uint32_t chunk_packets=0;bool done=false;
        // Wait up to ~0.5 s for MinChunkPackets, then take every whole packet already
        // encoded up to chunk_cap (16 KB, adaptive down to 4 KB after failures).
        for(int waited=0;waited<25;++waited){
            uint32_t available;{std::lock_guard<std::mutex> ring_lock(ring_mutex_);available=ring_.Packets();}
            done=encode_done_.load()==generation;
            if(available>=MeetingUpload::MinChunkPackets || done || !(state_==RecordingState||state_==Stopping))break;
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        {std::lock_guard<std::mutex> ring_lock(ring_mutex_);length=ring_.Take(block,chunk_cap,&chunk_packets);}
        if(length){
            // count/path are preparation locals. Publish identity with timing in Request.
            auto path="/v2/chunk/"+session_+"/"+std::to_string(count);
            // Same identity/bytes only; each retry uses a fresh connection (Request
            // resets the handle on failure). Bounded by the receiver's inactivity
            // window and by capture: QueueFull turns state_ to Error and stops this.
            const int64_t first_started=esp_timer_get_time();uint32_t failed=0;
            ok=Request(path,block,length,response,MeetingDiagnostics::ChunkHttp,1,count);
            while(!ok && (state_==RecordingState||state_==Stopping) && MeetingUpload::ShouldRetry(first_started,esp_timer_get_time(),++failed)){
                vTaskDelay(pdMS_TO_TICKS(MeetingUpload::RetryDelayMs(failed)));
                ok=Request(path,block,length,response,MeetingDiagnostics::ChunkHttp,uint8_t(failed+1),count);
            }
            if(!ok)Fatal(MeetingDiagnostics::HttpFinal);
            if(ok){auto root=cJSON_Parse(response.c_str());auto next=cJSON_GetObjectItem(root,"next"),id=cJSON_GetObjectItem(root,"session");ok=cJSON_IsNumber(next)&&next->valueint==int(count+1)&&cJSON_IsString(id)&&session_==id->valuestring;
                {std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.request.ack=ok?1:2;diagnostic_.current.request.ack_session_match=cJSON_IsString(id)&&session_==id->valuestring;diagnostic_.current.request.expected_next=count+1;diagnostic_.current.request.actual_next=cJSON_IsNumber(next)?next->valueint:-1;diagnostic_.timing.Finish(esp_timer_get_time(),queue_?uxQueueMessagesWaiting(queue_):0,ok?3:4,diagnostic_.current.request.code,diagnostic_.current.request.http);if(!ok)FatalLocked(MeetingDiagnostics::AckInvalid);}
                cJSON_Delete(root);}
            if(!ok){ResetHttp();state_=Error;break;}
            chunk_cap=MeetingUpload::NextChunkCap(chunk_cap,failed>0);
            mbedtls_sha256_update(&hash,block,length);bytes+=length;++count;packets+=chunk_packets;
            {std::lock_guard<std::mutex> lock(mutex_);diagnostic_.current.acked_bytes=bytes;diagnostic_.current.acked_chunks=count;}
        }
        {std::lock_guard<std::mutex> lock(mutex_);if(state_==RecordingState&&esp_timer_get_time()-last_input_>1000000){FatalLocked(MeetingDiagnostics::NoInputProgress);state_=Error;}}
        if(state_==Stopping && done){std::lock_guard<std::mutex> ring_lock(ring_mutex_);if(!ring_.Packets())break;}
    }
    Application::GetInstance().GetAudioService().EnableMeetingCapture(false);
    // Every captured sample must have been encoded and every packet acknowledged.
    {std::lock_guard<std::mutex> lock(mutex_);if(state_==Stopping&&(encode_done_.load()!=generation||encoded_samples_.load()!=samples_||
        packets!=(samples_+MeetingUpload::OpusFrameSamples-1)/MeetingUpload::OpusFrameSamples)){FatalLocked(MeetingDiagnostics::DrainMismatch);state_=Error;}}
    if(ok&&state_==Stopping&&bytes>0){
        uint8_t digest[32];mbedtls_sha256_finish(&hash,digest);char hex[65];for(int i=0;i<32;++i)snprintf(hex+2*i,3,"%02x",digest[i]);
        std::string body="{\"count\":"+std::to_string(count)+",\"bytes\":"+std::to_string(bytes)+",\"sha256\":\""+hex+"\",\"packets\":"+std::to_string(packets)+",\"samples\":"+std::to_string(samples_)+"}";
        ok=Request("/v2/finish/"+session_,body.data(),body.size(),response,MeetingDiagnostics::FinishHttpPhase);
        if(!ok)ok=Request("/v2/finish/"+session_,body.data(),body.size(),response,MeetingDiagnostics::FinishHttpPhase,2);
        if(!ok)Fatal(MeetingDiagnostics::FinishHttp);
        if(ok){ok=ApplyResponse(response);if(!ok)Fatal(MeetingDiagnostics::FinishResponse);}
    }else ok=false;
    if(!ok){
        ResetHttp(); // Includes invalid start/finish identities before abort.
        Fatal(MeetingDiagnostics::EmptyStop); // fallback only; never replaces a more specific first cause
        state_=Error;
        {std::lock_guard<std::mutex> lock(mutex_);diagnostic_.Add(MeetingDiagnostics::AbortCleanup,MeetingDiagnostics::Unknown,esp_timer_get_time(),Error,Error);}
        auto summary=DiagnosticSummary();
        Request("/v2/abort/"+session_,summary.data(),summary.size(),response,MeetingDiagnostics::AbortHttp);
    }
    mbedtls_sha256_free(&hash);
    StopEncoder(); // before the (possibly long) processing poll and the result UI
    if(state_==Processing)ReleasePower(); // Upload complete; remote processing polls need no continuous hold.
    // Saved/error exits retain the demand until release.Now() closes the final HTTP handle.
    int failures=0;
    while(state_==Processing){
        vTaskDelay(pdMS_TO_TICKS(2000));
        if(Request("/v2/status/"+session_,nullptr,0,response,MeetingDiagnostics::StatusHttpPhase,uint8_t(failures+1))){failures=0;bool applied=ApplyResponse(response);if(!applied)ResetHttp();if(!applied && state_==Error)Fatal(MeetingDiagnostics::StatusResponse);}
        else if(++failures>=3){Fatal(MeetingDiagnostics::StatusHttp);state_=Error;}
    }
    release.Now(); // Release before publishing worker_running_=false to the next Start.
    {std::lock_guard<std::mutex> lock(mutex_);UpdateDiagnosticLocked();diagnostic_.Add(MeetingDiagnostics::WorkerEnded,MeetingDiagnostics::Unknown,esp_timer_get_time(),state_,state_);diagnostic_dump_requested_=true;worker_running_=false;}
}
#if CONFIG_HEAP_TASK_TRACKING
// meeting.21-diag only: who holds internal RAM. Per task, current and peak bytes in
// heaps with MALLOC_CAP_INTERNAL vs MALLOC_CAP_SPIRAM (peak = sum of per-heap peaks,
// an upper bound of the true combined peak). Buffers live in PSRAM; never called
// from capture or with the scheduler suspended.
static void LogHeapTasks(const char* tag) {
    static constexpr size_t MaxTasks=48, MaxHeaps=MaxTasks*8;
    static constexpr size_t MaxBlocks=6000;
    static task_stat_t* tasks=nullptr;static heap_stat_t* heaps=nullptr;static heap_task_block_t* blocks=nullptr;
    if(!tasks){tasks=static_cast<task_stat_t*>(heap_caps_calloc(MaxTasks,sizeof(task_stat_t),MALLOC_CAP_SPIRAM));
               heaps=static_cast<heap_stat_t*>(heap_caps_calloc(MaxHeaps,sizeof(heap_stat_t),MALLOC_CAP_SPIRAM));
               blocks=static_cast<heap_task_block_t*>(heap_caps_calloc(MaxBlocks,sizeof(heap_task_block_t),MALLOC_CAP_SPIRAM));}
    if(!tasks||!heaps||!blocks)return;
    std::memset(tasks,0,MaxTasks*sizeof(task_stat_t));std::memset(heaps,0,MaxHeaps*sizeof(heap_stat_t)); // fill count is not returned
    heap_all_tasks_stat_t all={};all.task_count=MaxTasks;all.stat_arr=tasks;all.heap_count=MaxHeaps;all.heap_stat_start=heaps;
    all.alloc_count=MaxBlocks;all.alloc_stat_start=blocks;
    if(heap_caps_get_all_task_stat(&all)!=ESP_OK)return;
    struct Row{const char* name;bool alive;size_t ic,ip,ec,ep;};Row rows[MaxTasks];size_t n=0,total_ic=0;
    for(size_t i=0;i<MaxTasks;++i){
        const auto& t=tasks[i];if(!t.name[0]&&!t.handle&&!t.overall_peak_usage)continue;
        Row r{t.name,t.is_alive,0,0,0,0};
        if(t.heap_stat)for(size_t h=0;h<t.heap_count;++h){const auto& hs=t.heap_stat[h];
            if(hs.caps&MALLOC_CAP_SPIRAM){r.ec+=hs.current_usage;r.ep+=hs.peak_usage;}
            else if(hs.caps&MALLOC_CAP_INTERNAL){r.ic+=hs.current_usage;r.ip+=hs.peak_usage;}}
        total_ic+=r.ic;rows[n++]=r;
        // Long-lived holders (v2): sizes of their live internal blocks, never contents
        // (these blocks may hold MQTT credentials or tokens).
        if(t.heap_stat&&(!std::strcmp(t.name,"activation")||!std::strcmp(t.name,"ipc0"))&&(!std::strcmp(tag,"end")||!std::strcmp(tag,"idle"))){
            uint32_t sizes[48];size_t m=0,total=0,count=0;
            for(size_t h=0;h<t.heap_count;++h){const auto& hs=t.heap_stat[h];if(!(hs.caps&MALLOC_CAP_INTERNAL)||(hs.caps&MALLOC_CAP_SPIRAM)||!hs.alloc_stat)continue;
                for(size_t k=0;k<hs.alloc_count;++k){uint32_t z=hs.alloc_stat[k].size;total+=z;++count;
                    if(m<48)sizes[m++]=z;else{auto mn=std::min_element(sizes,sizes+m);if(z>*mn)*mn=z;}}}
            std::sort(sizes,sizes+m,[](uint32_t a,uint32_t b){return a>b;});
            char list[400];size_t o=0;list[0]=0;
            for(size_t k=0;k<m&&o<sizeof(list)-12;++k)o+=snprintf(list+o,sizeof(list)-o,k?",%u":"%u",unsigned(sizes[k]));
            ESP_LOGI("MeetingDiag","heap_blocks tag=%s name=%s internal_blocks=%u internal_bytes=%u largest48=%s",tag,t.name,unsigned(count),unsigned(total),list);
        }
    }
    std::sort(rows,rows+n,[](const Row& a,const Row& b){return a.ip>b.ip;});
    ESP_LOGI("MeetingDiag","heap_tasks tag=%s tasks=%u int_tracked_cur=%u int_free=%u int_min=%u int_largest=%u psram_free=%u",tag,unsigned(n),unsigned(total_ic),
        unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),unsigned(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL)),
        unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)),unsigned(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
    for(size_t i=0;i<n&&i<24;++i){const auto& r=rows[i];if(r.ip<256&&r.ic<256)continue;
        ESP_LOGI("MeetingDiag","heap_task tag=%s name=%s alive=%u int_cur=%u int_peak=%u ext_cur=%u ext_peak=%u",tag,r.name,unsigned(r.alive),
            unsigned(r.ic),unsigned(r.ip),unsigned(r.ec),unsigned(r.ep));}
}
#endif
void MeetingRecorder::Tick() {
#if CONFIG_HEAP_TASK_TRACKING
    {   static int64_t last=-60000000;const int64_t now=esp_timer_get_time(); // first idle snapshot at ~60 s uptime
        const int64_t every=(Protected()||state_.load()==Processing)?30000000:120000000;
        if(now-last>=every){last=now;LogHeapTasks(Protected()?"rec":"idle");}}
#endif
    if(!Protected() && diagnostic_dump_requested_.exchange(false))DumpDiagnostics();
    State state; uint32_t samples; std::string url;
    {std::lock_guard<std::mutex> lock(mutex_);state=state_.load();samples=samples_;url=url_;}
    std::lock_guard<std::mutex> ui_lock(ui_mutex_);
    if(state==Complete||state==Saved||state==Error){
        const int64_t now=esp_timer_get_time();
        if(!result_since_)result_since_=now;
        else if(!dismissed_&&now-result_since_>=ResultAutoDismissUs){
            dismissed_=true; // back to the normal screen after 10 min
            if(panel_){DisplayLockGuard guard(Board::GetInstance().GetDisplay());lv_obj_add_flag(static_cast<lv_obj_t*>(panel_),LV_OBJ_FLAG_HIDDEN);}
            return;
        }
    } else result_since_=0;
    if(state==Idle||dismissed_)return;
    auto display=Board::GetInstance().GetDisplay();DisplayLockGuard guard(display);
    if(!panel_){
        auto p=lv_obj_create(lv_layer_top());panel_=p;
        lv_obj_set_size(p,display->width(),display->height()-28);lv_obj_align(p,LV_ALIGN_BOTTOM_MID,0,0);
        lv_obj_set_style_bg_color(p,lv_color_white(),0);lv_obj_set_style_bg_opa(p,LV_OPA_COVER,0);
        lv_obj_set_style_border_width(p,0,0);lv_obj_set_style_radius(p,0,0);lv_obj_set_style_pad_all(p,8,0);
        lv_obj_remove_flag(p,LV_OBJ_FLAG_SCROLLABLE);
        auto theme=static_cast<LvglTheme*>(display->GetTheme());
        lv_obj_set_style_text_font(p,theme->text_font()->font(),0);
        label_=lv_label_create(p);lv_obj_set_style_text_color(static_cast<lv_obj_t*>(label_),lv_color_black(),0);
        lv_obj_set_width(static_cast<lv_obj_t*>(label_),display->width()-16);lv_obj_align(static_cast<lv_obj_t*>(label_),LV_ALIGN_TOP_MID,0,0);
    }
    lv_obj_remove_flag(static_cast<lv_obj_t*>(panel_),LV_OBJ_FLAG_HIDDEN);
    if(state!=drawn_state_&&qr_){lv_obj_delete(static_cast<lv_obj_t*>(qr_));qr_=nullptr;}
    const char* message="";
    switch(state){
        case Starting:message="正在连接接收端";break;
        case RecordingState:message="正在记录 · 单击BOOT停止";break;
        case Stopping:message="已停止 · 校验完整录音";break;
        case Processing:message="录音完整 · 得到处理中";break;
        case Complete:message="会议结果 · 扫码查看";break;
        case Saved:message="录音已保存 · 尚未授权处理";break;
        case Error:message="记录异常 · 请查看Mac\n不完整录音不会提交";break;
        default:break;
    }
    char text[180];snprintf(text,sizeof(text),"%s\n%02lu:%02lu / %02lu:%02lu",message,(unsigned long)(samples/16000/60),(unsigned long)(samples/16000%60),
        (unsigned long)(MeetingPcm::MaxSeconds/60),(unsigned long)(MeetingPcm::MaxSeconds%60));
    lv_label_set_text(static_cast<lv_obj_t*>(label_),text);
    if(state==Complete&&!qr_){
        // Reuse the already-linked QR encoder; render with LVGL canvas.
        uint8_t temp[qrcodegen_BUFFER_LEN_FOR_VERSION(10)], code[qrcodegen_BUFFER_LEN_FOR_VERSION(10)];
        bool valid=qrcodegen_encodeText(url.c_str(),temp,code,qrcodegen_Ecc_MEDIUM,1,10,qrcodegen_Mask_AUTO,true);
        int modules=valid?qrcodegen_getSize(code):0;
        int available=std::min(display->width()-32,display->height()-110);
        int scale=modules?available/(modules+8):0;
        if(scale<2){state_=Error;return;}
        int side=(modules+8)*scale; if(side%2)++side;
        qr_pixels_.assign(side*side,0xffff);
        for(int y=0;y<modules;++y)for(int x=0;x<modules;++x)if(qrcodegen_getModule(code,x,y))
            for(int dy=0;dy<scale;++dy)for(int dx=0;dx<scale;++dx)
                qr_pixels_[((y+4)*scale+dy)*side+(x+4)*scale+dx]=0;
        auto qr=lv_canvas_create(static_cast<lv_obj_t*>(panel_));qr_=qr;
        lv_canvas_set_buffer(qr,qr_pixels_.data(),side,side,LV_COLOR_FORMAT_RGB565);
        lv_obj_align(qr,LV_ALIGN_BOTTOM_MID,0,0);

    }
    drawn_state_=state;
}

bool MeetingRecorder::DismissResult(bool include_processing) {
    const State s=state_.load();
    if(Protected()||s==Idle||dismissed_.load())return false;
    if(s==Processing&&!include_processing)return false;
    Dismiss();return true;
}
void MeetingRecorder::Dismiss() {
    if (Protected()) return;
    std::lock_guard<std::mutex> ui_lock(ui_mutex_);
    dismissed_=true;
    if(panel_) { DisplayLockGuard guard(Board::GetInstance().GetDisplay());lv_obj_add_flag(static_cast<lv_obj_t*>(panel_),LV_OBJ_FLAG_HIDDEN); }
}

void MeetingRecorder::UpdateDiagnosticLocked() {
    diagnostic_.current.sampled=samples_;
    diagnostic_.current.state=state_.load();
    diagnostic_.current.queue_frames=queue_?uxQueueMessagesWaiting(queue_):0;
}
void MeetingRecorder::FatalLocked(MeetingDiagnostics::Reason reason) {
    UpdateDiagnosticLocked();
    if(diagnostic_.Fatal(reason,esp_timer_get_time()))diagnostic_dump_requested_=true;
}
void MeetingRecorder::Fatal(MeetingDiagnostics::Reason reason) {
    std::lock_guard<std::mutex> lock(mutex_);FatalLocked(reason);
}
namespace {
cJSON* DiagnosticContext(const MeetingDiagnostics::Context& c) {
    auto j=cJSON_CreateObject();if(!j)return nullptr;
    bool valid=true;
    auto number=[&](const char* key,double value){if(valid)valid=cJSON_AddNumberToObject(j,key,value)!=nullptr;};
    number("state",c.state);number("seq",c.seq);number("sampled",c.sampled);number("enqueued",c.enqueued);
    number("dequeued",c.dequeued);number("tx_pcm_bytes",c.tx_pcm_bytes);number("acked_bytes",c.acked_bytes);
    number("acked_chunks",c.acked_chunks);number("queue_frames",c.queue_frames);number("capture_code",c.capture_code);
    number("stop_source",c.stop_source);number("stop_us",c.stop_us);number("stop_requests",c.stop_requests);
    number("phase",c.request.phase);number("attempt",c.request.attempt);number("elapsed_us",c.request.elapsed_us);
    number("error_kind",c.request.error_kind);number("code",c.request.code);number("http",c.request.http);
    number("ack",c.request.ack);number("expected_next",c.request.expected_next);number("actual_next",c.request.actual_next);
    number("request_written",c.request.payload_written);
    valid=valid && cJSON_AddBoolToObject(j,"in_flight",c.request.in_flight)!=nullptr
                && cJSON_AddBoolToObject(j,"ack_session_match",c.request.ack_session_match)!=nullptr;
    if(!valid){cJSON_Delete(j);return nullptr;}
    return j;
}
std::string DiagnosticJson(const MeetingDiagnostics& d,bool include_current) {
    using JsonOwner=std::unique_ptr<cJSON,decltype(&cJSON_Delete)>;
    using TextOwner=std::unique_ptr<char,decltype(&cJSON_free)>;
    try {
        JsonOwner j(cJSON_CreateObject(),cJSON_Delete);
        if(!j || !cJSON_AddNumberToObject(j.get(),"v",1)
              || !cJSON_AddStringToObject(j.get(),"session",d.session)
              || !cJSON_AddBoolToObject(j.get(),"first_present",d.first.present))return {};
        if(d.first.present){
            JsonOwner first(DiagnosticContext(d.first.context),cJSON_Delete);
            if(!first || !cJSON_AddNumberToObject(first.get(),"reason",d.first.reason)
                      || !cJSON_AddNumberToObject(first.get(),"time_us",d.first.time_us)
                      || !cJSON_AddItemToObject(j.get(),"first",first.get()))return {};
            first.release(); // Parent owns the child only after AddItem succeeds.
        }
        if(include_current){
            JsonOwner current(DiagnosticContext(d.current),cJSON_Delete);
            if(!current || !cJSON_AddItemToObject(j.get(),"current",current.get()))return {};
            current.release();
            if(!cJSON_AddNumberToObject(j.get(),"event_count",d.event_count)
               || !cJSON_AddNumberToObject(j.get(),"dropped_events",d.dropped_events)
               || !cJSON_AddNumberToObject(j.get(),"attempts_failed",d.attempts_failed)
               || !cJSON_AddNumberToObject(j.get(),"attempts_recovered",d.attempts_recovered)
               || !cJSON_AddNumberToObject(j.get(),"max_attempt",d.max_attempt))return {};
        }
        TextOwner printed(cJSON_PrintUnformatted(j.get()),cJSON_free);
        if(!printed)return {};
        return std::string(printed.get());
    } catch(const std::bad_alloc&) {
        // Includes the final std::string copy. All cJSON ownership is already scoped.
        return {};
    }
}

}
std::string MeetingRecorder::DiagnosticSummary() {
    MeetingDiagnostics copy;
    {std::lock_guard<std::mutex> lock(mutex_);UpdateDiagnosticLocked();copy=diagnostic_;}
    auto json=DiagnosticJson(copy,false);
    // Failure to format diagnostics must fall back to the original empty abort body.
    if(json.size()>2048)json.clear();
    return json;
}
void MeetingRecorder::DumpDiagnostics() {
    // Never called by capture. Retained RAM can be replayed by BOOT/status without HTTP.
    for(int slot=0;slot<2;++slot){
        MeetingDiagnostics copy;
        {std::lock_guard<std::mutex> lock(mutex_);UpdateDiagnosticLocked();copy=slot?previous_diagnostic_:diagnostic_;}
        if(!copy.session[0] && copy.event_count==0)continue;
        copy.timing.current=copy.timing.current.Snapshot(esp_timer_get_time(),copy.current.queue_frames);
        auto json=DiagnosticJson(copy,true);
        if(json.empty()) {
            // This marker uses no cJSON allocation. No snapshot/events/end for this failed slot.
            ESP_LOGI("MeetingDiag","format_failed slot=%d sid=%s first_present=%u",slot,copy.session,unsigned(copy.first.present));
            continue;
        }
        ESP_LOGI("MeetingDiag","snapshot slot=%d %s",slot,json.c_str());
        for(uint32_t i=0;i<copy.event_count;++i){const auto& e=copy.EventAt(i);
            char micros[21];
            ESP_LOGI("MeetingDiag","event sid=%s n=%lu us=%s kind=%u source=%u before=%u after=%u seq=%lu phase=%u attempt=%u elapsed_us=%lu err_kind=%u code=%ld http=%ld ack=%u",
                copy.session,(unsigned long)copy.EventNumber(i),DiagnosticMicros(e.time_us,micros),unsigned(e.kind),unsigned(e.source),unsigned(e.before),unsigned(e.after),(unsigned long)e.seq,unsigned(e.phase),unsigned(e.attempt),(unsigned long)e.elapsed_us,unsigned(e.error_kind),(long)e.code,(long)e.http,unsigned(e.ack));
        }
        // Text records avoid cJSON allocations. A failed bounded format has no end.
        char line[768];bool timing_ok=true;
        auto emit_timing=[&](const char* role,uint32_t index,const MeetingRequestTiming& t) {
            if(!FormatMeetingTiming(line,sizeof(line),copy.session,role,index,t)){timing_ok=false;return;}
            ESP_LOGI("MeetingDiag","%s",line);
        };
        ESP_LOGI("MeetingDiag","timing_meta sid=%s v=1 count=%lu total=%lu successful=%lu slow_success=%lu max_q=%lu first=%u",
            copy.session,(unsigned long)copy.timing.count,(unsigned long)copy.timing.total,(unsigned long)copy.timing.successful,(unsigned long)copy.timing.slow_success,(unsigned long)copy.timing.max_queue,unsigned(copy.first.present));
        if(copy.first.present)emit_timing("first",0,copy.timing.first);
        emit_timing("current",0,copy.timing.current);
        for(uint32_t i=0;i<copy.timing.count;++i)emit_timing("recent",i,copy.timing.At(i));
        for(unsigned i=0;i<7;++i){char digits[21];ESP_LOGI("MeetingDiag","timing_total sid=%s stage=%u us=%s",copy.session,i,DiagnosticMicros(copy.timing.total_us[i],digits));}
        if(!timing_ok){ESP_LOGI("MeetingDiag","format_failed slot=%d sid=%s first_present=%u",slot,copy.session,unsigned(copy.first.present));continue;}
        ESP_LOGI("MeetingDiag","end sid=%s count=%lu dropped=%lu failed=%lu recovered=%lu max_attempt=%u",copy.session,(unsigned long)copy.event_count,(unsigned long)copy.dropped_events,
            (unsigned long)copy.attempts_failed,(unsigned long)copy.attempts_recovered,unsigned(copy.max_attempt));
    }
    // Bytes never touched since creation (FreeRTOS high-water mark, ESP-IDF counts bytes).
    {TaskHandle_t task;{std::lock_guard<std::mutex> lock(mutex_);task=encoder_task_;}
        // A parked/deleted encoder reported its own high-water mark before parking.
        if(task&&!encoder_parked_.load())ESP_LOGI("MeetingDiag","encoder_stack_min_free=%u",unsigned(uxTaskGetStackHighWaterMark(task)));
        else if(encoder_stack_min_free_.load())ESP_LOGI("MeetingDiag","encoder_stack_min_free=%u",unsigned(encoder_stack_min_free_.load()));}
#if CONFIG_HEAP_TASK_TRACKING
    LogHeapTasks("end");
#endif
    Application::GetInstance().DumpPowerSave();
}
