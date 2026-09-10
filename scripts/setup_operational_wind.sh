#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SYSTEM_PYTHON="${PYTHON3:-python3}"

if ! command -v "$SYSTEM_PYTHON" >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3, or set PYTHON3 to its path." >&2
    exit 2
fi

"$SYSTEM_PYTHON" -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

echo "Operational environment created at $PROJECT_DIR/.venv"
