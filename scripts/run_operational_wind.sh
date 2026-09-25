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

# A station that fails still leaves the others' products updated, so the charts
# are rebuilt either way and the estimator's exit status is reported at the end.
status=0
"$PYTHON" "$PROJECT_DIR/operational_wind.py" --output-dir "$OUTPUT_DIR" "$@" || status=$?

# Charts, validation, and the optional web copy. WINDPROXY_PLOTS=0 skips them;
# WINDPROXY_WEB_DIR names a folder to publish into, such as an NGINX root.
if [ "${WINDPROXY_PLOTS:-1}" != "0" ]; then
    "$PYTHON" "$PROJECT_DIR/scripts/build_wind_plot.py" \
        --input "$OUTPUT_DIR/wind_estimates.csv" \
        --status "$OUTPUT_DIR/status.json" \
        --output-dir "$OUTPUT_DIR" \
        || echo "Chart build failed; estimates were still updated." >&2
fi

exit "$status"
