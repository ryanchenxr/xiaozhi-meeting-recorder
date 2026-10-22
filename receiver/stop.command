#!/bin/zsh
set -eu
cd "${0:A:h}"
.venv/bin/python - <<'PY'
from pathlib import Path
import json, urllib.request
p=Path('private/control.json')
if not p.exists():
    print('本项目程序未运行。')
else:
    c=json.loads(p.read_text())
    req=urllib.request.Request('http://127.0.0.1:'+str(c['port'])+'/api/stop',data=b'',headers={'X-CSRF-Token':c['csrf']},method='POST')
    try:
        urllib.request.urlopen(req,timeout=10).read()
        print('已请求正常关闭本项目程序及专用浏览器。')
    except Exception:
        print('未连接到本项目程序，请检查启动终端；不会关闭日常Chrome。')
PY
