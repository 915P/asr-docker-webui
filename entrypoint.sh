#!/usr/bin/env bash
# 无参数 / webui 子命令 -> 启动 WebUI；否则当作 CLI 转录参数。
set -euo pipefail

if [ "${1:-}" = "webui" ]; then
    shift
    exec python /app/webui.py "$@"
fi

if [ "$#" -eq 0 ]; then
    exec python /app/webui.py
fi

exec python /app/transcribe.py "$@"