#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README 完成本项目依赖安装。'
  exit 1
fi
exec .venv/bin/python -u server.py
