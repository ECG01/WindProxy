#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNNER="$PROJECT_DIR/scripts/run_operational_wind.sh"
LOG_FILE="$PROJECT_DIR/logs/operational_wind.log"
CRON_LINE="*/10 * * * * $RUNNER >> $LOG_FILE 2>&1"
TEMP_CRONTAB="$(mktemp -t windproxy-crontab.XXXXXX)"
trap 'rm -f "$TEMP_CRONTAB"' EXIT HUP INT TERM

crontab -l > "$TEMP_CRONTAB" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$TEMP_CRONTAB"; then
    echo "WindProxy cron entry is already installed."
    exit 0
fi

mkdir -p "$PROJECT_DIR/logs"
printf '\n# WindProxy: poll Arecibo and Rincon CDIP spectra\n%s\n' "$CRON_LINE" >> "$TEMP_CRONTAB"
crontab "$TEMP_CRONTAB"
echo "Installed: $CRON_LINE"
