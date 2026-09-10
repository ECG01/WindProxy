#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
OUTPUT_DIR="$PROJECT_DIR/data/operational"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

if [ ! -x "$PYTHON" ]; then
    echo "Missing $PYTHON. Run scripts/setup_operational_wind.sh first." >&2
    exit 2
fi

exec "$PYTHON" "$PROJECT_DIR/operational_wind.py" --output-dir "$OUTPUT_DIR" "$@"
