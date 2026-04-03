#!/bin/bash
PORT=8876
DIR="$(cd "$(dirname "$0")" && pwd)"
SCREEN_NAME="v4live"
REPO_DIR="$(cd "$DIR/../.." && pwd)"
if [ -x "$REPO_DIR/venv/bin/python" ]; then
  PYTHON_BIN="$REPO_DIR/venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if lsof -i :"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "[OK] 端口 $PORT 已在监听，无需重启"
  echo "访问地址: http://<REDACTED_HOST>:$PORT"
  exit 0
fi

if screen -ls | grep -q "$SCREEN_NAME"; then
  screen -S "$SCREEN_NAME" -X quit 2>/dev/null
  sleep 1
fi

screen -dmS "$SCREEN_NAME" "$PYTHON_BIN" "$DIR/server.py"
sleep 1

if lsof -i :"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "[OK] 已在 screen '$SCREEN_NAME' 中启动"
  echo "访问地址: http://<REDACTED_HOST>:$PORT"
  echo "管理: screen -r $SCREEN_NAME"
else
  echo "[FAIL] 启动失败，请检查端口 $PORT 是否被占用"
  exit 1
fi
