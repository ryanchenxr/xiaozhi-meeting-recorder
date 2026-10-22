# 小智会议录音台（xiaozhi-meeting-recorder）

把一块几十块钱的 ESP32-S3 开发板，改造成能录 **60 分钟会议**、录完自动出纪要、屏幕弹出二维码扫码查看的"AI 会议录音台"。

- 说"你好小智，开始记录会议"开始录音；说"你好小智"，再说"结束会议记录"（或按一下按键）结束录音；
- 板子边录边用 Opus 压缩，通过局域网分块上传到一台常开的电脑（接收端），断网可以自动重试；
- 接收端校验录音完整后，交给转写和纪要服务，再把分享链接做成二维码，显示到板子屏幕上。

> 这是一个**个人原型**，基于开源项目 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 修改，全程借助 AI 编程助手（Claude Code）完成。
> **想自己做一台？** 把 [docs/AI-DEV-GUIDE.md](docs/AI-DEV-GUIDE.md) 整份交给你的 AI 编程助手。里面有我们踩过的 22 个坑，以及每个坑的根因、解法和验证方法。

## 仓库结构

| 目录 | 内容 |
|---|---|
| `firmware/` | 固件：xiaozhi-esp32 v2.2.4 + 会议录音功能（`main/meeting_*`、`application.cc` 等） |
| `firmware-tests/` | 在电脑上运行的单元测试，以及传输模拟（不需要板子） |
| `receiver/` | 局域网接收端（Python）：接收、校验、解码、转码、生成二维码、自动发现 |
| `docs/AI-DEV-GUIDE.md` | 给 AI 看的避坑开发指南 |

想看我们具体改了哪些地方，可以按顺序看 git 历史：第 1 个提交是原样导入的上游 v2.2.4，第 2 个提交是板子卖家提供的屏幕方向修改，第 3 个提交起是会议录音功能。

## 硬件

我们使用征辰 AI Camera（ESP32-S3 N16R8、ES7210 麦克风、ES8311 喇叭、320×240 ST7789 屏幕，固件板型 `zhengchen-cam`）。其他 xiaozhi 支持的 ESP32-S3 板子也可以参考，**PSRAM 至少 8 MB**。

## 快速开始

### 1. 接收端（macOS）

```bash
cd receiver
cp device.example.json device.local.json && chmod 600 device.local.json
# 编辑 device.local.json：token 换成一串随机字符串
# 生成方法：python3 -c "import secrets;print(secrets.token_urlsafe(32))"
./setup-mac.command      # 安装 opus、ffmpeg、Python 依赖，并运行自检
./start.command
```

录音保存在 `receiver/private/device-sessions/<会话>/original.wav`（60 分钟的录音另有一份 `original.m4a`）。`allow_processing` 为 `false` 时只保存，不交给转写服务。

### 2. 固件（ESP-IDF 5.5.3）

```bash
cd firmware
cp meeting_config.example.h meeting_config.local.h   # 填入和接收端一致的 token
python scripts/release.py zhengchen-cam
```

烧录方法参考 xiaozhi-esp32 的官方文档。建议只写入应用分区，详见指南中的"坑 22"。

### 3. 测试

```bash
cd firmware-tests && clang++ -std=c++17 -I../firmware/main voice_control_test.cc -o /tmp/t && /tmp/t
cd receiver && python -m unittest -q
python firmware-tests/sim_opus.py receiver   # 真实 Opus 包 + 真实接收端的端到端模拟
```

## 关于"交给转写服务"这一步

我们自己用的是"得到大脑"。它没有公开的上传音频接口，所以原型里用一个专用浏览器，调用官网网页自带的上传流程。这部分依赖官网前端的内部实现，官网一改版就会失效，也可能不符合服务条款，所以**公开版没有包含这段脚本**：`receiver/site-worker.js` 只是一个占位文件。

公开版仍然包含：
- 完整的录音、传输、校验和存档流程；
- 通过得到**官方开放平台 API** 读回笔记、生成不含原音的分享链接（`receiver/official.py`）。凭证放在 `receiver/openapi.local.json`，格式见 `openapi.example.json`。

如果你使用的服务有官方上传接口，在 `site-worker.js` / `browser.py` 这里接入即可。也可以只用到"录音保存到电脑"这一步，再手动上传。

## 安全提醒

- `meeting_config.local.h`、`device.local.json`、`openapi.local.json` 和 `private/` 目录都已经被 `.gitignore` 排除，**不要提交**；
- 接收端只接受局域网请求；自动发现使用 HMAC 认证，设备密钥不会在网络上传输。

## 致谢与引用

- **[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)**（MIT License，Copyright (c) 2025 Shenzhen Xinzhi Future Technology Co., Ltd. and Project Contributors）：固件基础，本仓库的 `firmware/` 基于它的 v2.2.4（commit `e77dedb`）。上游原始许可证保留在 `firmware/LICENSE`。
- 征辰 AI Camera 卖家提供的 v2.2.4 源码包，其中包含屏幕方向修改（第 2 个提交）。
- [Opus](https://opus-codec.org/)（BSD）、[ESP-IDF](https://github.com/espressif/esp-idf)（Apache 2.0）、[FFmpeg](https://ffmpeg.org/)、[Playwright](https://playwright.dev/)（Apache 2.0）、[qrcode](https://github.com/lincolnloop/python-qrcode)（BSD）。

## 许可证

[MIT](LICENSE)。使用、修改、分发时，请保留 LICENSE 中的版权声明。

本项目是一个原型，与商业录音产品在稳定性、远场收音、实时转写等方面还有很大差距。分享出来，是想说明：现在普通人借助 AI，复刻一个产品原型已经很容易了。
