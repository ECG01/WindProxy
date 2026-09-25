#!/bin/sh
# Install the ten-minute polling job, preserving the rest of the crontab.
#
#   ./scripts/install_cron.sh                      # estimates and charts only
#   ./scripts/install_cron.sh /var/www/windproxy   # also publish to a web folder
#
# Re-running replaces any earlier WindProxy entry, so the web folder can be
# changed or dropped by running it again.
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNNER="$PROJECT_DIR/scripts/run_operational_wind.sh"
LOG_FILE="$PROJECT_DIR/logs/operational_wind.log"
WEB_DIR="${1:-}"

if [ -n "$WEB_DIR" ]; then
    case "$WEB_DIR" in
        /*) ;;
        *) echo "Use an absolute path for the web folder." >&2; exit 2 ;;
    esac
    mkdir -p "$WEB_DIR" 2>/dev/null || true
    if [ ! -w "$WEB_DIR" ]; then
        echo "Cannot write to $WEB_DIR. Create it and give $(id -un) write access, e.g.:" >&2
        echo "  sudo mkdir -p $WEB_DIR && sudo chown $(id -un) $WEB_DIR" >&2
        exit 2
    fi
    CRON_LINE="*/10 * * * * WINDPROXY_WEB_DIR=$WEB_DIR $RUNNER >> $LOG_FILE 2>&1"
else
    CRON_LINE="*/10 * * * * $RUNNER >> $LOG_FILE 2>&1"
fi

TEMP_CRONTAB="$(mktemp -t windproxy-crontab.XXXXXX)"
trap 'rm -f "$TEMP_CRONTAB"' EXIT HUP INT TERM

crontab -l > "$TEMP_CRONTAB" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$TEMP_CRONTAB"; then
    echo "WindProxy cron entry is already installed."
    exit 0
fi

# Drop any previous WindProxy entry and its comment before adding the new one.
grep -Fv "$RUNNER" "$TEMP_CRONTAB" | grep -Fvx "# WindProxy: poll Arecibo and Rincon CDIP spectra" > "$TEMP_CRONTAB.new" || true
mv "$TEMP_CRONTAB.new" "$TEMP_CRONTAB"

mkdir -p "$PROJECT_DIR/logs"
printf '\n# WindProxy: poll Arecibo and Rincon CDIP spectra\n%s\n' "$CRON_LINE" >> "$TEMP_CRONTAB"
crontab "$TEMP_CRONTAB"
echo "Installed: $CRON_LINE"
