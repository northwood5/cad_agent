#!/usr/bin/env bash
# ============================================================
# start.sh — Start the CAx Agent backend (conda env with in-process FreeCAD)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"

# ---- Locate the conda 'cax' env (in-process FreeCAD lives here) ----
CONDA_ROOT="${CONDA_ROOT:-$HOME/miniforge3}"
CAX_PY="$CONDA_ROOT/envs/cax/bin/python"
if [ ! -x "$CAX_PY" ]; then
  echo "[ERROR] conda env 'cax' not found at $CONDA_ROOT/envs/cax"
  echo "  Create it with:"
  echo "    conda create -y -n cax -c conda-forge python=3.11 freecad"
  echo "    conda run -n cax pip install -r backend/requirements.txt"
  exit 1
fi

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
echo "  ╔══════════════════════════════════════╗"
echo "  ║  CAx Agent  →  http://localhost:8000  ║"
echo "  ╚══════════════════════════════════════╝"
echo "  FreeCAD: in-process (conda env 'cax')"
echo ""

cd "$BACKEND"
exec "$CAX_PY" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
