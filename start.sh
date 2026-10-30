#!/usr/bin/env bash
# Start the conversion API (uvicorn cad2obj.api:app).
#   port     listen port   (default 8200)
#   host     bind address  (default localhost)
#   VENV_DIR venv directory (default .venv)
# Extra args are passed to uvicorn, e.g. ./start.sh --workers 4
set -euo pipefail
cd "$(dirname "$0")"

PORT="${port:-8200}"
HOST="${host:-localhost}"
VENV_DIR="${VENV_DIR:-.venv}"

if [ ! -x "$VENV_DIR/bin/uvicorn" ]; then
    echo "error: uvicorn not found in $VENV_DIR - run ./install.sh first" >&2
    exit 1
fi

exec "$VENV_DIR/bin/uvicorn" cad2obj.api:app --host "$HOST" --port "$PORT" "$@"
