#!/usr/bin/env bash
# Start the Algo Trading Terminal
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Setup venv if not present
if [[ ! -d .venv ]]; then
    echo "[INFO] Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install dependencies
pip install -q -r backend/requirements.txt

# Copy .env if not exists
if [[ ! -f .env && -f .env.example ]]; then
    cp .env.example .env
fi

echo ""
echo "=============================================="
echo "  ALGO TRADING TERMINAL"
echo "  http://localhost:8000"
echo "=============================================="
echo ""

exec uvicorn backend.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload \
    --log-level "${LOG_LEVEL:-info}"
