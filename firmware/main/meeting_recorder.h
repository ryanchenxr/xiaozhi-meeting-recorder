#pragma once
#include <atomic>
#include <esp_http_client.h>
#include "meeting_pcm.h"
#include "meeting_opus_ring.h"
#include "meeting_diagnostics.h"
#include <mutex>
#include <string>
#include <vector>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

// One producer: AudioService's existing input task. One network consumer.
class MeetingRecorder {
public:
    static MeetingRecorder& Get();
    std::string Start(MeetingDiagnostics::Source source=MeetingDiagnostics::Mcp);
    std::string Stop(MeetingDiagnostics::Source source=MeetingDiagnostics::Unknown);
    std::string Status();
    bool Protected() const;
    bool PowerRequired() const { return performance_required_.load(); }
    unsigned PowerState() const { return unsigned(state_.load()); }
    bool Recording() const { return state_.load()==RecordingState; }
    bool BlockOperation();
    void Feed(const std::vector<int16_t>& pcm, int channels);
    void InputFailed(MeetingDiagnostics::Reason reason=MeetingDiagnostics::InputRead, int code=0);
    void RequestDiagnosticDump() { diagnostic_dump_requested_=true; }
    void Dismiss();
    // meeting.23: hide a visible result panel (QR/saved/error; optionally also the
    // "processing" panel) and report whether one was hidden. BOOT click and the wake
    // word use it to return to the normal screen; results also auto-hide after 10 min.
    bool DismissResult(bool include_processing);
    void Tick(); // application task only, LVGL under DisplayLockGuard
private:
    enum State { Idle, Starting, RecordingState, Stopping, Processing, Complete, Saved, Error };
    using Frame=MeetingPcm::Frame;
    MeetingPcm pcm_;
    std::vector<uint16_t> qr_pixels_;
    bool FlushFrame();
    std::atomic<State> state_{Idle};
    std::mutex mutex_;
    std::mutex ui_mutex_;
    QueueHandle_t queue_=nullptr;
    StaticQueue_t queue_control_{}; // internal BSS; only sample storage uses PSRAM
    uint8_t* send_buffer_=nullptr; // PSRAM, MeetingUpload::MaxChunkBytes; upload worker only
    // Opus path (meeting.16). One persistent encoder task (PSRAM stack) drains queue_,
    // encodes 60 ms packets into ring_; the upload worker sends whole packets.
    std::mutex ring_mutex_;          // guards ring_ and generation_ changes
    MeetingOpusRing ring_;           // storage in PSRAM, MeetingUpload::OpusRingBytes
    void* opus_encoder_=nullptr;     // encoder task only after creation
    TaskHandle_t encoder_task_=nullptr;
    std::atomic<uint32_t> generation_{0};      // bumped by Start under ring_mutex_
    std::atomic<uint32_t> encode_done_{0};     // generation whose end-of-stream was encoded
    std::atomic<uint32_t> encoded_samples_{0}; // real (unpadded) samples pushed this generation
    bool EnsureEncoderLocked();
    void EncoderTask();
    // meeting.20: the encoder task (PSRAM stack, uses FPU/PIE) exists only during a
    // session. Its coprocessor save area lives on that PSRAM stack; a lazy FPU
    // switch while the cache is disabled faulted once ("Cache error", 2026-10-19,
    // ~4 s after an abort). After finish/abort the worker parks and deletes it,
    // which releases its coprocessor ownership; the next Start creates a new one.
    static constexpr uint16_t EncoderExitMarker=0xFFFF; // Frame.count, never a real count
    std::atomic<bool> encoder_parked_{false};
    std::atomic<bool> encoder_exit_requested_{false}; // set by StopEncoder until deleted/recreated
    std::atomic<uint32_t> encoder_stack_min_free_{0};
    void StopEncoder();
    bool PushEndOfStream();
    bool worker_running_=false;
    // meeting.21: one persistent upload task, created once right after boot activation
    // (boot HTTPS done, internal heap still contiguous) and woken per session. A
    // per-session xTaskCreate needed 8 KB of contiguous internal heap, which after a
    // voice conversation was fragmented to a 6.9 KB largest block and failed as
    // TaskCreate (meeting.21-diag, 2026-10-19). A static BSS stack would cost the same
    // 8 KB during boot HTTPS too; the stack must stay internal (the task's lwIP/HTTP/
    // cJSON paths use FPU/PIE, see meeting.20 Cache error).
    TaskHandle_t upload_task_=nullptr;
    bool EnsureUploadTaskLocked();
    void UploadLoop();
    // meeting.22: receiver found per session by LAN broadcast (meeting_discovery.h);
    // falls back to the last receiver found, then to MEETING_DEVICE_URL. Upload task only.
    std::string receiver_base_, last_receiver_;
    std::string DiscoverReceiver();
    void ResolveReceiver();
public:
    void PrepareUploadTask(); // Application, after MAIN_EVENT_ACTIVATION_DONE
private:
    std::atomic<bool> performance_required_{false};
    void ReleasePower();
    uint32_t samples_=0;
    int64_t last_input_=0;
    std::string session_, url_;
    void* panel_=nullptr;
    void* label_=nullptr;
    void* qr_=nullptr;
    State drawn_state_=Idle;
    std::atomic<bool> dismissed_{false};
    int64_t result_since_=0; // Tick only: when the current result screen appeared
    static constexpr int64_t ResultAutoDismissUs=600000000;
    MeetingDiagnostics diagnostic_, previous_diagnostic_;
    std::atomic<bool> diagnostic_dump_requested_{false};
    void UpdateDiagnosticLocked();
    void FatalLocked(MeetingDiagnostics::Reason reason);
    void Fatal(MeetingDiagnostics::Reason reason);
    std::string DiagnosticSummary();
    void DumpDiagnostics();
    // Only the upload worker touches the handle. Never used by capture/UI tasks.
    esp_http_client_handle_t http_client_=nullptr;
    uint32_t connection_count_=0; // guarded by mutex_; actual ON_CONNECTED events
    static esp_err_t HttpEvent(esp_http_client_event_t* event);
    void ResetHttp();
    void Run();
    bool Request(const std::string& path, const void* data, size_t size, std::string& response, MeetingDiagnostics::Phase phase, uint8_t attempt=1, uint32_t seq=0);
    bool ApplyResponse(const std::string& response);
};
