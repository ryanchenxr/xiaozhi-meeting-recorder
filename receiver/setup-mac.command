#!/bin/zsh
# 在新 Mac（例如办公室 Mac mini）上准备会议接收程序。可以重复运行；不删除任何数据。
set -u
cd "${0:A:h}"
ok() { print -P "%F{green}✓%f $1"; }
bad() { print -P "%F{red}✗%f $1"; FAILED=1; }
FAILED=0
BREW=/opt/homebrew/bin/brew
[[ -x $BREW ]] || BREW=/usr/local/bin/brew

print '== 1/5 Homebrew =='
if [[ -x $BREW ]]; then ok "Homebrew: $BREW"
else bad '没有 Homebrew。请先按 https://brew.sh 页面上的一行命令安装（需要输入本机密码），装完再运行本脚本。'; exit 1; fi

print '== 2/5 opus、ffmpeg、python =='
for f in opus ffmpeg python@3.12; do
  if $BREW list --versions $f >/dev/null 2>&1; then ok "$f 已安装"
  else print "正在安装 $f …"; $BREW install $f && ok "$f 安装完成" || bad "$f 安装失败"; fi
done
PREFIX=$($BREW --prefix)
[[ -e $PREFIX/lib/libopus.dylib ]] && ok "libopus: $PREFIX/lib/libopus.dylib" || bad '找不到 libopus.dylib'
[[ -x $PREFIX/bin/ffmpeg && -x $PREFIX/bin/ffprobe ]] && ok 'ffmpeg / ffprobe 可用' || bad '找不到 ffmpeg / ffprobe'
if [[ $PREFIX != /opt/homebrew ]]; then
  print "提示：Homebrew 不在 /opt/homebrew。启动前请设置 MEETING_LIBOPUS=$PREFIX/lib/libopus.dylib、MEETING_FFMPEG=$PREFIX/bin/ffmpeg、MEETING_FFPROBE=$PREFIX/bin/ffprobe。"
fi

print '== 3/5 Python 环境（.venv）=='
PY=$PREFIX/opt/python@3.12/bin/python3.12
if [[ ! -x .venv/bin/python ]]; then
  $PY -m venv .venv && ok '已创建 .venv' || bad '创建 .venv 失败'
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt && ok '依赖已安装（playwright、qrcode）' || bad '依赖安装失败'
# 不运行 "playwright install"：程序使用本机已安装的 Google Chrome。

print '== 4/5 Google Chrome =='
[[ -d "/Applications/Google Chrome.app" ]] && ok '已安装 Google Chrome' || bad '没有 Google Chrome，请先从 https://www.google.com/chrome 安装'

print '== 5/5 设备配置 device.local.json =='
if [[ -f device.local.json ]]; then
  chmod 600 device.local.json
  .venv/bin/python - <<'PY'
import json
c=json.load(open('device.local.json'))
assert len(c.get('token',''))>=32, 'token 太短'
print('  bind =',c.get('bind'),'| port =',c.get('port'),'| allow_processing =',c.get('allow_processing'))
if c.get('bind')!='auto': print('  提示：bind 不是 "auto"，换网络后板子会找不到。')
PY
  [[ $? -eq 0 ]] && ok 'device.local.json 可用（权限 600）' || bad 'device.local.json 内容不对'
else bad '缺少 device.local.json：把 device.example.json 复制为 device.local.json，填入和固件一致的 token（不要发给别人）'; fi

print '== 自检 =='
.venv/bin/python -m unittest -q test_discovery test_opus_receiver 2>&1 | tail -2
if [[ $FAILED -eq 0 ]]; then
  print -P '%F{green}准备完成。%f 下一步：双击 start.command，在弹出的专用 Chrome 里用手机扫码登录得到。'
else
  print -P '%F{red}还有问题，见上面的 ✗。%f'
fi
