"""Build HTML and PNG charts of the operational wind estimates, with validation.

Reads the CSV written by operational_wind.py, pairs each buoy with the nearest
CARICOOS weather station (see validation_wind.py), and writes for each station:

- ``wind_plot_<station>.html``: a self-contained interactive page that opens
  directly in a browser.
- ``wind_plot_<station>.png``: a static figure for reports, when matplotlib is
  installed (``pip install -r requirements-plot.txt``).

The HTML needs only the standard library. Station data is fetched over the
network; pass ``--no-validation`` to plot the buoy estimates alone.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from validation_wind import (
    VALIDATION_STATIONS,
    Observations,
    comparison_stats,
    fetch_observations,
    match_to_buoy,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "wind_plot_template.html"
DEFAULT_DIR = PROJECT_DIR / "data" / "operational"
STATION_NAMES = {"249p1": "Arecibo", "181p1": "Rincon"}


def number(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    return round(float(value), 3)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_rows(csv_path: Path, station_id: str, days: float | None) -> tuple[list[dict[str, Any]], str]:
    with csv_path.open(newline="", encoding="utf-8") as stream:
        source = [row for row in csv.DictReader(stream) if row["station_id"] == station_id]
    if not source:
        return [], STATION_NAMES.get(station_id, station_id)
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


def plot_window(rows: list[dict[str, Any]], days: float | None) -> tuple[datetime, datetime]:
    """The buoy record's span, or the last ``days`` up to now when there is none."""
    now = datetime.now(timezone.utc)
    if rows:
        start, end = parse_utc(rows[0]["t"]), max(parse_utc(rows[-1]["t"]) + timedelta(minutes=30), now)
        return start, end
    return now - timedelta(days=days or 7.0), now


def validate(rows: list[dict[str, Any]], obs: Observations | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Station values matched to each buoy row, and summary statistics."""
    if obs is None or obs.error:
        return [{} for _ in rows], {}
    matched = match_to_buoy([parse_utc(r["t"]) for r in rows], obs)
    out = [
        {"u": None if u is None else round(u, 3), "d": None if d is None else round(d, 1)}
        for u, d in matched
    ]
    published = [(r, m) for r, m in zip(rows, out) if r["qc"] != "rejected"]
    speed_all = [(r["u"], m["u"]) for r, m in published if r["u"] is not None and m["u"] is not None]
    speed_good = [(r["u"], m["u"]) for r, m in published if r["qc"] == "good" and r["u"] is not None and m["u"] is not None]
    direction_all = [(r["d"], m["d"]) for r, m in published if r["d"] is not None and m["d"] is not None]
    stats = {
        "speed_all": comparison_stats(speed_all),
        "speed_good": comparison_stats(speed_good),
        "direction_all": comparison_stats(direction_all, circular=True),
    }
    return out, stats


def station_notices(status_path: Path, station_id: str, obs: Observations | None) -> str:
    """Flag a buoy whose latest polling run failed, and an unreadable station."""
    notices = []
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        info = status.get("stations", {}).get(station_id, {})
        if info.get("error"):
            notices.append(
                f"<strong>CDIP {html.escape(station_id)} had no data in the latest run</strong> "
                f"({html.escape(status.get('run_at_utc', ''))}). The buoy may be offline or under "
                "maintenance; the station observations are still shown."
            )
    if obs is not None and obs.error:
        notices.append(
            f"<strong>{html.escape(obs.name)} could not be read.</strong> {html.escape(obs.error)}"
        )
    return "".join(
        f'  <div class="notice"><span class="dot"></span><div>{text}</div></div>\n' for text in notices
    )


def render(
    rows: list[dict[str, Any]],
    station_id: str,
    station_name: str,
    source: str,
    notices: str,
    obs: Observations | None = None,
    matched: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    payload = {
        "buoy": rows,
        "matched": matched or [],
        "stats": stats or {},
        "obs": [],
        "meta": {},
    }
    if obs is not None:
        payload["meta"] = {"name": obs.name, "id": obs.station_id, "height": obs.height,
                           "url": obs.source_url, "error": obs.error}
        payload["obs"] = [
            {"t": iso(t), "u": None if u is None else round(u, 3), "d": d}
            for t, u, d in zip(obs.times, obs.speed_m_s, obs.direction_deg)
        ]
    # "</" is escaped so a stray "</script>" in the data cannot end the script block.
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
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


def render_png(
    path: Path,
    rows: list[dict[str, Any]],
    station_id: str,
    station_name: str,
    obs: Observations | None,
    matched: list[dict[str, Any]],
    stats: dict[str, Any],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    buoy, station, ref, ink, muted, grid = "#2a78d6", "#eb6834", "#8a98a3", "#0f1a22", "#6f7f8b", "#e2e8ed"
    times = [parse_utc(r["t"]) for r in rows]
    fig = plt.figure(figsize=(13, 9), dpi=130, facecolor="white")
    layout = fig.add_gridspec(3, 2, width_ratios=[2.6, 1], hspace=0.35, wspace=0.18)
    ax_u, ax_d, ax_h = (fig.add_subplot(layout[i, 0]) for i in range(3))
    ax_sc = fig.add_subplot(layout[0:2, 1])
    ax_tx = fig.add_subplot(layout[2, 1])
    for ax in (ax_u, ax_d, ax_h, ax_sc):
        ax.grid(color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#b9c4cc")
        ax.tick_params(colors=muted, labelsize=8)

    def series(key: str, only_published: bool = True):
        good = [(t, r[key]) for t, r in zip(times, rows) if r[key] is not None and r["qc"] == "good"]
        quest = [(t, r[key]) for t, r in zip(times, rows) if r[key] is not None and r["qc"] == "questionable"]
        return good, quest

    def gapped(points: list[tuple[datetime, float | None]]):
        xs, ys, prev = [], [], None
        for t, v in points:
            if v is None:
                prev = None
                continue
            if prev is not None and (t - prev) > timedelta(hours=1):
                xs.append(t)
                ys.append(float("nan"))
            xs.append(t)
            ys.append(v)
            prev = t
        return xs, ys

    label = obs.name if obs else "Station"
    if obs is not None and not obs.error:
        ax_u.plot(*gapped(list(zip(obs.times, obs.speed_m_s))), color=station, linewidth=1.1, label=f"{label} observed")
        ax_d.scatter(obs.times, [d if d is not None else float("nan") for d in obs.direction_deg], s=4, color=station, alpha=0.7, linewidths=0, label=f"{label} observed")
    published = [(t, r["u"] if r["qc"] != "rejected" else None) for t, r in zip(times, rows)]
    ax_u.plot(*gapped(published), color=buoy, linewidth=1.8, label="Buoy U10 estimate")
    for ax, key in ((ax_u, "u"), (ax_d, "d")):
        good, quest = series(key)
        if good:
            ax.scatter(*zip(*good), s=16, color=buoy, edgecolors="white", linewidths=0.5, zorder=3, label="QC good" if ax is ax_u else None)
        if quest:
            ax.scatter(*zip(*quest), s=16, facecolors="white", edgecolors=buoy, linewidths=1.1, zorder=3, label="QC questionable" if ax is ax_u else None)
    ax_u.axhline(7, color=ref, linestyle="--", linewidth=1, label="7 m/s direction threshold")
    ax_h.plot(*gapped([(t, r["hs"]) for t, r in zip(times, rows)]), color=buoy, linewidth=1.8)

    ax_u.set_ylabel("Wind speed (m/s)", color=ink, fontsize=9)
    ax_u.set_ylim(bottom=0)
    ax_d.set_ylabel("Wind from (° true)", color=ink, fontsize=9)
    ax_d.set_ylim(0, 360)
    ax_d.set_yticks([0, 90, 180, 270, 360], ["N 0", "E 90", "S 180", "W 270", "N 360"])
    ax_h.set_ylabel("Hs (m)", color=ink, fontsize=9)
    ax_h.set_ylim(bottom=0)
    span = [t for t in times] + (list(obs.times) if obs and not obs.error else [])
    for ax in (ax_u, ax_d, ax_h):
        if span:
            ax.set_xlim(min(span), max(span))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))
    ax_u.legend(loc="upper left", fontsize=7.5, frameon=False, ncol=3)

    pairs = [(m["u"], r["u"], r["qc"]) for r, m in zip(rows, matched)
             if r["qc"] != "rejected" and r["u"] is not None and m.get("u") is not None]
    top = max([4.0] + [max(a, b) + 0.5 for a, b, _ in pairs])
    ax_sc.plot([0, top], [0, top], color=ref, linestyle="--", linewidth=1)
    for qc, face in (("good", buoy), ("questionable", "white")):
        pts = [(a, b) for a, b, q in pairs if q == qc]
        if pts:
            ax_sc.scatter(*zip(*pts), s=20, facecolors=face, edgecolors=buoy if face == "white" else "white", linewidths=1, label=f"QC {qc}")
    ax_sc.set_xlim(0, top)
    ax_sc.set_ylim(0, top)
    ax_sc.set_aspect("equal")
    ax_sc.set_xlabel(f"{label} observed (m/s)", color=ink, fontsize=9)
    ax_sc.set_ylabel("Buoy U10 estimate (m/s)", color=ink, fontsize=9)
    ax_sc.set_title("Buoy vs station", color=ink, fontsize=10, loc="left")
    if pairs:
        ax_sc.legend(loc="upper left", fontsize=7.5, frameon=False)

    if not rows:
        for ax in (ax_h, ax_sc):
            ax.text(0.5, 0.5, "No buoy records in this window", transform=ax.transAxes,
                    ha="center", va="center", fontsize=9, color=muted)
    ax_tx.axis("off")

    def line(name: str, s: dict[str, float] | None, unit: str, digits: int) -> str:
        if not s:
            return f"{name:<22}  N=0"
        r = "" if s.get("r") is None else f"  r={s['r']:.2f}"
        return f"{name:<22}  N={s['n']:<4} bias={s['bias']:+.{digits}f}{unit}  RMSE={s['rmse']:.{digits}f}{unit}{r}"

    text = [
        "Validation (bias = buoy − station)",
        line("Speed, QC good", stats.get("speed_good"), " m/s", 2),
        line("Speed, all published", stats.get("speed_all"), " m/s", 2),
        line("Direction", stats.get("direction_all"), "°", 0),
        "",
        f"Station: {obs.name if obs else 'none'}",
        f"{obs.height if obs else ''}",
    ]
    if obs is not None and obs.error:
        text.append(f"Station unreadable: {obs.error[:70]}")
    text.append("Land anemometer vs open-water neutral U10:")
    text.append("a steady offset is expected.")
    ax_tx.text(0, 1, "\n".join(text), va="top", ha="left", fontsize=8, family="monospace", color=ink)

    first = times[0] if times else (obs.times[0] if obs and obs.times else None)
    last = times[-1] if times else (obs.times[-1] if obs and obs.times else None)
    window = f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC" if first and last else ""
    fig.suptitle(f"{station_name} buoy winds  ·  CDIP {station_id}  ·  {window}", x=0.06, ha="left", fontsize=12, color=ink, fontweight="bold")
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--station-id",
        action="append",
        dest="station_ids",
        help="CDIP station id; repeat for several. Default: every buoy with a validation station.",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_DIR / "wind_estimates.csv")
    parser.add_argument("--status", type=Path, default=DEFAULT_DIR / "status.json")
    parser.add_argument("--output-dir", type=Path, help="Default: the input CSV's directory.")
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="Days to plot, counted back from the newest record. 0 plots everything.",
    )
    parser.add_argument("--no-validation", action="store_true", help="Skip fetching station observations.")
    parser.add_argument("--no-png", action="store_true", help="Write the HTML page only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        print(f"Missing {args.input}. Run scripts/run_operational_wind.sh first.", file=sys.stderr)
        return 2
    output_dir = args.output_dir or args.input.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = str(args.input.resolve().relative_to(PROJECT_DIR))
    except ValueError:
        source = args.input.name

    png_ok = not args.no_png
    if png_ok:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            print("matplotlib is not installed; skipping PNG. Install with: "
                  ".venv/bin/python -m pip install -r requirements-plot.txt", file=sys.stderr)
            png_ok = False

    failures = 0
    for station_id in args.station_ids or list(VALIDATION_STATIONS):
        rows, station_name = load_rows(args.input, station_id, args.days or None)
        obs = None
        if not args.no_validation:
            start, end = plot_window(rows, args.days or None)
            obs = fetch_observations(station_id, start, end)
        has_obs = obs is not None and not obs.error and len(obs.times) >= 2
        if len(rows) < 2 and not has_obs:
            print(f"{station_name}: nothing to plot (need 2 buoy records or station observations).", file=sys.stderr)
            failures += 1
            continue
        matched, stats = validate(rows, obs)
        notices = station_notices(args.status, station_id, obs)
        html_path = output_dir / f"wind_plot_{station_id}.html"
        html_path.write_text(render(rows, station_id, station_name, source, notices, obs, matched, stats), encoding="utf-8")
        written = [html_path.name]
        if png_ok:
            png_path = output_dir / f"wind_plot_{station_id}.png"
            render_png(png_path, rows, station_id, station_name, obs, matched, stats)
            written.append(png_path.name)
        obs_note = "no station" if obs is None else (f"station error: {obs.error}" if obs.error else f"{len(obs.times)} station readings")
        speed = stats.get("speed_all")
        stat_note = f", speed bias {speed['bias']:+.2f} m/s RMSE {speed['rmse']:.2f}" if speed else ""
        print(f"{station_name}: {len(rows)} buoy records, {obs_note}{stat_note} -> {', '.join(written)} in {output_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
