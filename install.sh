#!/usr/bin/env bash
# Create the venv and install the requirements. Idempotent: reuses an existing venv.
#   PYTHON   python interpreter to build with  (default python3)
#   VENV_DIR venv directory                    (default .venv)
#   REQS     requirements file                 (default requirements.txt)
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
REQS="${REQS:-requirements.txt}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found on PATH" >&2
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "creating $VENV_DIR with $PYTHON"
    "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REQS"

echo "done: $VENV_DIR/bin/python"
