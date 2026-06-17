#!/usr/bin/env bash
# ============================================================
# start.sh — Start the CAD Agent backend
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"
BACKEND="$SCRIPT_DIR/backend"

# ---- Activate venv ----
if [ ! -f "$VENV/bin/activate" ]; then
  echo "[ERROR] venv not found at $VENV. Run setup first:"
  echo "  python3 -m venv venv && venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi
source "$VENV/bin/activate"

# ---- Optional: load .env for API keys ----
ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "[INFO] Loading $ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
fi

# ---- Check config ----
CONFIG="$BACKEND/config/llm_config.yaml"
if [ ! -f "$CONFIG" ]; then
  echo "[ERROR] Config not found: $CONFIG"
  exit 1
fi

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║  CAD Agent  →  http://localhost:8000  ║"
echo "  ╚══════════════════════════════════╝"
echo ""
echo "  设置 API Key 的三种方式:"
echo "    1. 编辑 backend/config/llm_config.yaml"
echo "    2. 在 .env 文件中: OPENAI_API_KEY=sk-..."
echo "    3. 在前端页面 [LLM 设置] 中填写"
echo ""

cd "$BACKEND"
exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload
