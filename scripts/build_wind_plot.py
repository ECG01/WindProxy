"""Build a standalone HTML chart of one station's operational wind estimates.

Reads the CSV written by operational_wind.py and writes a single self-contained
HTML page (wind speed, wind direction, and significant wave height against
time) that opens directly in a browser. Uses only the standard library, so it
runs with any Python 3.9+ and needs no network.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "wind_plot_template.html"
DEFAULT_DIR = PROJECT_DIR / "data" / "operational"


def number(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    return round(float(value), 3)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_rows(csv_path: Path, station_id: str, days: float | None) -> tuple[list[dict[str, Any]], str]:
    with csv_path.open(newline="", encoding="utf-8") as stream:
        source = [row for row in csv.DictReader(stream) if row["station_id"] == station_id]
    if not source:
        return [], station_id
    source.sort(key=lambda row: row["time_utc"])
    if days is not None:
        cutoff = parse_utc(source[-1]["time_utc"]) - timedelta(days=days)
        source = [row for row in source if parse_utc(row["time_utc"]) >= cutoff]
    rows = [
        {
            "t": row["time_utc"],
            "u": number(row["estimated_u10_m_s"]),
            "d": number(row["estimated_wind_direction_deg_from"]),
            "hs": number(row["significant_wave_height_m"]),
            "tp": number(row["peak_period_s"]),
            "qc": row["qc_status"],
            "why": row["qc_reasons"],
            "dc": row["direction_confidence"],
        }
        for row in source
    ]
    return rows, source[-1]["station_name"]


def station_notices(status_path: Path, station_id: str) -> str:
    """Flag other stations whose latest polling run failed, e.g. a buoy offline."""
    if not status_path.exists():
        return ""
    status = json.loads(status_path.read_text(encoding="utf-8"))
    notices = []
    for other_id, info in status.get("stations", {}).items():
        if other_id == station_id or not info.get("error"):
            continue
        notices.append(
            '  <div class="notice"><span class="dot"></span><div>'
            f"<strong>{html.escape(info.get('station_name', other_id))} ({html.escape(other_id)}) "
            "had no data in the latest run.</strong> CDIP could not serve its real-time file "
            f"at {html.escape(status.get('run_at_utc', 'the last run'))}; the buoy may be offline "
            "or under maintenance.</div></div>\n"
        )
    return "".join(notices)


def render(rows: list[dict[str, Any]], station_id: str, station_name: str, source: str, notices: str) -> str:
    # "</" is escaped so a stray "</script>" in the data cannot end the script block.
    data = json.dumps(rows, separators=(",", ":")).replace("</", "<\\/")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__STATION_NAME__", html.escape(station_name))
        .replace("__STATION_ID__", html.escape(station_id))
        .replace("__NOTICES__", notices)
        .replace("__SOURCE__", html.escape(source))
        .replace("__GEN__", generated)
        .replace("__DATA__", data)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", default="249p1", help="CDIP station id (default 249p1, Arecibo).")
    parser.add_argument("--input", type=Path, default=DEFAULT_DIR / "wind_estimates.csv")
    parser.add_argument("--status", type=Path, default=DEFAULT_DIR / "status.json")
    parser.add_argument("--output", type=Path, help="Default: data/operational/wind_plot_<station>.html")
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="Days to plot, counted back from the newest record. 0 plots everything.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        print(f"Missing {args.input}. Run scripts/run_operational_wind.sh first.", file=sys.stderr)
        return 2
    rows, station_name = load_rows(args.input, args.station_id, args.days or None)
    if len(rows) < 2:
        print(f"Need at least 2 records for station {args.station_id}; found {len(rows)}.", file=sys.stderr)
        return 1
    output = args.output or args.input.parent / f"wind_plot_{args.station_id}.html"
    try:
        source = str(args.input.resolve().relative_to(PROJECT_DIR))
    except ValueError:
        source = args.input.name
    page = render(rows, args.station_id, station_name, source, station_notices(args.status, args.station_id))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    print(f"Wrote {len(rows)} records for {station_name} to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
