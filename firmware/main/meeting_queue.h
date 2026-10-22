#pragma once
#include "meeting_pcm.h"
#include "meeting_upload_policy.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <esp_heap_caps.h>
#include <esp_log.h>

// Default xQueueCreate uses INTERNAL RAM even when malloc supports PSRAM.
// The caller's StaticQueue_t is internal BSS; task-only PCM storage is external.
inline QueueHandle_t CreateMeetingQueue(StaticQueue_t& control) {
    constexpr size_t frames=MeetingUpload::QueueFrames; // ~15 s, see meeting_upload_policy.h
    constexpr size_t bytes=frames*sizeof(MeetingPcm::Frame);
    auto* storage=static_cast<uint8_t*>(heap_caps_malloc(bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if (!storage) {
        ESP_LOGE("Meeting", "PCM allocation failed: bytes=%u PSRAM-largest=%u internal-largest=%u",
                 unsigned(bytes), unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)),
                 unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)));
        return nullptr;
    }
    auto queue=xQueueCreateStatic(frames,sizeof(MeetingPcm::Frame),storage,&control);
    if (!queue) {
        heap_caps_free(storage);
        ESP_LOGE("Meeting", "PCM static queue initialization failed");
        return nullptr;
    }
    ESP_LOGI("Meeting", "PCM queue ready: %u frames, %u bytes in PSRAM",unsigned(frames),unsigned(bytes));
    return queue; // Lifetime is the singleton recorder; reused across sessions.
}
