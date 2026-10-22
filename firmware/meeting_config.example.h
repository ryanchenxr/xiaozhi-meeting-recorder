// 复制为 meeting_config.local.h 后再编译（该文件已被 .gitignore 排除，不要提交）。
#pragma once
// 找不到接收端时的兜底地址；正常情况下板子会通过局域网自动发现接收端。
#define MEETING_DEVICE_URL "http://192.168.1.100:8767"
// 与接收端 device.local.json 里的 token 一致，至少 32 位随机字符串。
// 生成方法：python3 -c "import secrets;print(secrets.token_urlsafe(32))"
#define MEETING_DEVICE_TOKEN "change-me-to-a-long-random-string"
