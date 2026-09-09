#!/bin/bash
set -euo pipefail
export PYTHONUNBUFFERED=1
export SGLANG_URL="${SGLANG_URL:-http://127.0.0.1:8000}"
MODEL="${QWEN_MODEL:-Qwen/Qwen3-VL-8B-Instruct}"

sglang() {
  python3 -m sglang.launch_server \
    --model-path "$MODEL" \
    --host 0.0.0.0 \
    --port 8000 \
    --trust-remote-code \
    --mem-fraction-static 0.82 \
    --context-length 4096 \
    --max-running-requests 32 \
    --chunked-prefill-size 4096
}

if [ "${QWEN_SERVERLESS:-0}" = "1" ]; then
  sglang &
  exec python3 -u /app/handler.py
fi
exec sglang
