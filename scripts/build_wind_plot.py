"""Build HTML and PNG charts of the operational wind estimates, with validation.

Reads the CSV written by operational_wind.py, pairs each buoy with the nearest
CARICOOS weather station (see validation_wind.py), and writes for each station:

- ``wind_plot_<station>.html``: a self-contained interactive page that opens
  directly in a browser.
- ``wind_plot_<station>.png``: a static figure for reports, when matplotlib is
  installed (``pip install -r requirements-plot.txt``).

The CARICOOS WRF 1 km and 2 km forecasts are sampled at each buoy and station
(see model_wind.py) and scored alongside the buoy estimate. Fetched model hours
are cached in ``<output>/model/``; ``--no-model`` skips them.

It also writes an ``index.html`` landing page linking every station. With
``--web-dir`` (or the ``WINDPROXY_WEB_DIR`` environment variable) the pages,
figures, and data products are then copied into that directory, such as a folder
served by NGINX; each file is replaced atomically so readers never see a
half-written page.

The HTML needs only the standard library. Station data is fetched over the
network; pass ``--no-validation`` to plot the buoy estimates alone.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_wind import MODELS, ModelSeries, load_series
from model_wind import update as update_model
from validation_wind import (
    VALIDATION_STATIONS,
    Observations,
    comparison_stats,
    fetch_observations,
    match_to_buoy,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "wind_plot_template.html"
INDEX_TEMPLATE = Path(__file__).resolve().parent / "wind_index_template.html"
DEFAULT_DIR = PROJECT_DIR / "data" / "operational"
STATION_NAMES = {"249p1": "Arecibo", "181p1": "Rincon"}
# Data products published next to the pages, when they exist.
PUBLISHED_DATA = ("latest.json", "status.json", "wind_estimates.csv", "wind_estimates_since_2026.csv")
COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


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


def validate(
    rows: list[dict[str, Any]],
    obs: Observations | None,
    models: dict[str, dict[str, ModelSeries | None]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Station and model values matched to each buoy row, and skill scores.

    Buoy rows cover a 30 minute sample from their timestamp, so the station is
    averaged over that window and the models are interpolated to its middle.
    Models are also scored against the station on their own hourly timeline,
    averaging the station over the surrounding hour, so a buoy that is offline
    does not stop the model validation.
    """
    models = models or {}
    times = [parse_utc(r["t"]) for r in rows]
    have_obs = obs is not None and not obs.error
    station = match_to_buoy(times, obs) if have_obs else [(None, None)] * len(rows)
    out = []
    for when, (su, sd) in zip(times, station):
        entry: dict[str, Any] = {"u": None if su is None else round(su, 3), "d": None if sd is None else round(sd, 1)}
        for key, series in models.items():
            value = series["buoy"].at(when + timedelta(minutes=15)) if series.get("buoy") else None
            entry[key] = None if value is None else {"u": round(value[0], 3), "d": round(value[1], 1)}
        out.append(entry)

    published = [(r, m) for r, m in zip(rows, out) if r["qc"] != "rejected"]

    def pairs(field: str, reference, candidates, good_only: bool = False):
        return [
            (r[field], ref)
            for r, m in candidates
            if (not good_only or r["qc"] == "good")
            and r[field] is not None
            and (ref := reference(m)) is not None
        ]

    speed_all = pairs("u", lambda m: m["u"], published)
    speed_good = pairs("u", lambda m: m["u"], published, good_only=True)
    direction_all = pairs("d", lambda m: m["d"], published)
    table = [
        {"group": "Speed vs station", "label": "Buoy estimate, QC good", "series": "buoy", "unit": "m/s", "stats": comparison_stats(speed_good)},
        {"group": "Speed vs station", "label": "Buoy estimate, all published", "series": "buoy", "unit": "m/s", "stats": comparison_stats(speed_all)},
    ]
    direction_rows = [
        {"group": "Direction vs station", "label": "Buoy estimate", "series": "buoy", "unit": "°", "stats": comparison_stats(direction_all, circular=True)},
    ]
    against_buoy = []
    scatter: dict[str, list[list[float]]] = {}

    for key, series in models.items():
        label = MODELS[key].label
        at_station = series.get("station")
        speed_pairs, direction_pairs = [], []
        if have_obs and at_station is not None:
            hours = at_station.times
            averaged = match_to_buoy([h - timedelta(minutes=30) for h in hours], obs)
            for hour, (su, sd) in zip(hours, averaged):
                value = at_station.at(hour)
                if value is None:
                    continue
                if su is not None:
                    speed_pairs.append((value[0], su))
                if sd is not None:
                    direction_pairs.append((value[1], sd))
        scatter[key] = [[round(o, 2), round(m, 2)] for m, o in speed_pairs]
        table.append({"group": "Speed vs station", "label": label, "series": key, "unit": "m/s", "stats": comparison_stats(speed_pairs)})
        direction_rows.append({"group": "Direction vs station", "label": label, "series": key, "unit": "°",
                               "stats": comparison_stats(direction_pairs, circular=True)})
        model_speed = [(m[key]["u"], r["u"]) for r, m in published if m.get(key) and r["u"] is not None]
        model_direction = [(m[key]["d"], r["d"]) for r, m in published if m.get(key) and r["d"] is not None]
        against_buoy.append({"group": "Speed vs buoy estimate", "label": label, "series": key, "unit": "m/s", "stats": comparison_stats(model_speed)})
        against_buoy.append({"group": "Direction vs buoy estimate", "label": label, "series": key, "unit": "°",
                             "stats": comparison_stats(model_direction, circular=True)})

    table += direction_rows + sorted(against_buoy, key=lambda row: row["group"], reverse=True)
    stats = {
        "speed_all": comparison_stats(speed_all),
        "speed_good": comparison_stats(speed_good),
        "direction_all": comparison_stats(direction_all, circular=True),
        "table": table,
        "scatter": scatter,
    }
    return out, stats


def load_models(station_id: str, start: datetime, end: datetime, cache_dir: Path,
                fetch: bool = True) -> dict[str, dict[str, ModelSeries | None]]:
    """Update the model cache for the window, then read each model at the buoy and station."""
    models = {}
    for key, source in MODELS.items():
        if fetch:
            try:
                print(update_model(source, start - timedelta(hours=1), end, cache_dir))
            except Exception as error:
                print(f"{source.label}: update failed, using cached hours ({type(error).__name__}: {error})", file=sys.stderr)
        buoy = load_series(source, station_id, start - timedelta(hours=1), end, cache_dir)
        station = load_series(source, f"{station_id}:station", start - timedelta(hours=1), end, cache_dir)
        if buoy or station:
            models[key] = {"buoy": buoy, "station": station}
    return models


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
    png_name: str | None = None,
    models: dict[str, dict[str, ModelSeries | None]] | None = None,
) -> str:
    payload = {
        "buoy": rows,
        "matched": matched or [],
        "stats": stats or {},
        "obs": [],
        "meta": {},
        "models": [
            {
                "key": key,
                "label": MODELS[key].label,
                "cell": series["buoy"].cell,
                "pts": [
                    {"t": iso(t), "u": round(math.hypot(u, v), 3),
                     "d": round((270.0 - math.degrees(math.atan2(v, u))) % 360.0, 1)}
                    for t, u, v in zip(series["buoy"].times, series["buoy"].u, series["buoy"].v)
                ],
            }
            for key, series in (models or {}).items()
            if series.get("buoy")
        ],
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
        .replace("__PNG_LINK__", f'<a href="{html.escape(png_name)}">PNG figure</a>' if png_name else "")
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
    models: dict[str, dict[str, ModelSeries | None]] | None = None,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    buoy, station, ref, ink, muted, grid = "#2a78d6", "#eb6834", "#8a98a3", "#0f1a22", "#6f7f8b", "#e2e8ed"
    times = [parse_utc(r["t"]) for r in rows]
    models = models or {}
    # Categorical slots 3 and 4 after buoy (1) and station (2); 2 km is also dashed.
    model_style = {"wrf1km": ("#1baf7a", "-", "s"), "wrf2km": ("#eda100", "--", "^")}
    fig = plt.figure(figsize=(14, 12), dpi=130, facecolor="white")
    layout = fig.add_gridspec(4, 2, width_ratios=[2.6, 1], height_ratios=[1, 1.15, 0.9, 1.05], hspace=0.38, wspace=0.18)
    ax_u, ax_d, ax_h = (fig.add_subplot(layout[i, 0]) for i in range(3))
    # Legend on top, the speed-vs-station scatter beneath it.
    ax_leg = fig.add_subplot(layout[0, 1])
    ax_sc = fig.add_subplot(layout[1:3, 1])
    ax_tx = fig.add_subplot(layout[3, :])
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
    for key, pair in models.items():
        at_buoy = pair.get("buoy")
        if at_buoy is None:
            continue
        color, dash, _ = model_style.get(key, ("#4a3aa7", "-", "D"))
        speeds = [math.hypot(u, v) for u, v in zip(at_buoy.u, at_buoy.v)]
        dirs = [(270.0 - math.degrees(math.atan2(v, u))) % 360.0 for u, v in zip(at_buoy.u, at_buoy.v)]
        ax_u.plot(*gapped(list(zip(at_buoy.times, speeds))), color=color, linestyle=dash, linewidth=1.5, label=f"{MODELS[key].label} at buoy")
    published = [(t, r["u"] if r["qc"] != "rejected" else None) for t, r in zip(times, rows)]
    ax_u.plot(*gapped(published), color=buoy, linewidth=1.8, label="Buoy U10 estimate")
    good, quest = series("u")
    if good:
        ax_u.scatter(*zip(*good), s=16, color=buoy, edgecolors="white", linewidths=0.5, zorder=3, label="QC good")
    if quest:
        ax_u.scatter(*zip(*quest), s=16, facecolors="white", edgecolors=buoy, linewidths=1.1, zorder=3, label="QC questionable")

    # Direction as a quiver: one lane per source, arrows toward where the wind
    # blows with north up, vector-averaged into time bins. Arrows share one
    # length so calm-wind directions stay readable; speed has its own panel.
    span_all = times + (list(obs.times) if obs and not obs.error else [])
    span_hours = (max(span_all) - min(span_all)).total_seconds() / 3600 if span_all else 24
    bin_hours = next((h for h in (1, 2, 3, 6, 12) if span_hours / h <= 70), 24)
    lanes = [("Buoy", buoy, [(t, r["u"], r["d"], r["qc"] == "good") for t, r in zip(times, rows) if r["qc"] != "rejected"])]
    if obs is not None and not obs.error:
        lanes.append(("Station", station, list(zip(obs.times, obs.speed_m_s, obs.direction_deg, [True] * len(obs.times)))))
    for key, pair in models.items():
        if pair.get("buoy"):
            m = pair["buoy"]
            lanes.append((MODELS[key].label, model_style.get(key, ("#4a3aa7",))[0],
                          [(t, math.hypot(u, v), (270.0 - math.degrees(math.atan2(v, u))) % 360.0, True) for t, u, v in zip(m.times, m.u, m.v)]))
    binned = [bin_vectors(samples, timedelta(hours=bin_hours)) for _, _, samples in lanes]
    for k, ((name, color, _), bins) in enumerate(zip(lanes, binned)):
        y = len(lanes) - 1 - k
        ax_d.axhline(y, color=grid, linewidth=0.8, zorder=0)
        for faded in (False, True):
            pts = [b for b in bins if b[4] == faded]
            if pts:
                unit = [(b[1] / b[3], b[2] / b[3]) if b[3] > 0 else (0.0, 0.0) for b in pts]
                ax_d.quiver([mdates.date2num(b[0]) for b in pts], [y] * len(pts), [u for u, _ in unit], [v for _, v in unit],
                            angles="uv", scale_units="inches", scale=1 / 0.2, pivot="middle", color=color,
                            alpha=0.4 if faded else 1.0, width=0.0024, headwidth=3.5, headlength=4, headaxislength=3.5, zorder=2)
    ax_d.set_ylim(-0.7, len(lanes) - 0.3)
    ax_d.set_yticks(range(len(lanes)), [name for name, _, _ in reversed(lanes)])
    ax_d.grid(axis="y", visible=False)
    ax_d.set_title(f"Wind direction: arrows point where the wind blows toward, north up "
                   f"({bin_hours} h vector means; speed is in the panel above)", color=ink, fontsize=8.5, loc="left")
    ax_u.axhline(7, color=ref, linestyle="--", linewidth=1, label="7 m/s direction threshold")
    ax_h.plot(*gapped([(t, r["hs"]) for t, r in zip(times, rows)]), color=buoy, linewidth=1.8)

    ax_u.set_ylabel("Wind speed (m/s)", color=ink, fontsize=9)
    ax_u.set_ylim(bottom=0)
    ax_h.set_ylabel("Hs (m)", color=ink, fontsize=9)
    ax_h.set_ylim(bottom=0)
    span = [t for t in times] + (list(obs.times) if obs and not obs.error else [])
    for ax in (ax_u, ax_d, ax_h):
        if span:
            ax.set_xlim(min(span), max(span))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))

    pairs = [(m["u"], r["u"], r["qc"]) for r, m in zip(rows, matched)
             if r["qc"] != "rejected" and r["u"] is not None and m.get("u") is not None]
    model_pairs = stats.get("scatter", {})
    values = [max(a, b) for a, b, _ in pairs] + [max(a, b) for pts in model_pairs.values() for a, b in pts]
    top = max([4.0] + [v + 0.5 for v in values])
    ax_sc.plot([0, top], [0, top], color=ref, linestyle="--", linewidth=1)
    for key, pts in model_pairs.items():
        if pts:
            color, _, marker = model_style.get(key, ("#4a3aa7", "-", "D"))
            ax_sc.scatter(*zip(*pts), s=14, marker=marker, color=color, alpha=0.75, linewidths=0, label=f"{MODELS[key].label} (hourly)")
    for qc, face in (("good", buoy), ("questionable", "white")):
        pts = [(a, b) for a, b, q in pairs if q == qc]
        if pts:
            ax_sc.scatter(*zip(*pts), s=20, facecolors=face, edgecolors=buoy if face == "white" else "white", linewidths=1, label=f"Buoy, QC {qc}", zorder=3)
    ax_sc.set_xlim(0, top)
    ax_sc.set_ylim(0, top)
    ax_sc.set_aspect("equal")
    ax_sc.set_xlabel(f"{label} observed (m/s)", color=ink, fontsize=9)
    ax_sc.set_ylabel("Estimate or model (m/s)", color=ink, fontsize=9)
    ax_sc.set_title("Wind speed vs station", color=ink, fontsize=10, loc="left")
    if pairs or any(model_pairs.values()):
        ax_sc.legend(loc="upper left", fontsize=7, frameon=False)

    if not rows:
        for ax in (ax_h,):
            ax.text(0.5, 0.5, "No buoy records in this window", transform=ax.transAxes,
                    ha="center", va="center", fontsize=9, color=muted)

    # One shared legend for the time series, beside them.
    ax_leg.axis("off")
    handles, labels = ax_u.get_legend_handles_labels()
    ax_leg.legend(handles, labels, loc="upper left", fontsize=8, frameon=False, title="Time series", title_fontsize=8.5)

    ax_tx.axis("off")

    def line(name: str, s: dict[str, float] | None, unit: str, digits: int) -> str:
        if not s:
            return f"  {name:<30} N=0"
        r = "" if s.get("r") is None else f"  r={s['r']:.2f}"
        return f"  {name:<30} N={s['n']:<5} bias={s['bias']:+.{digits}f}{unit:<4} RMSE={s['rmse']:.{digits}f}{unit}{r}"

    text = ["Validation   (bias = candidate − reference)"]
    group = None
    for row in stats.get("table", []):
        if row["group"] != group:
            group = row["group"]
            text.append(group)
        speed = row["unit"] == "m/s"
        text.append(line(row["label"], row["stats"], " m/s" if speed else "°", 2 if speed else 0))
    notes = [f"Station: {obs.name if obs else 'none'}, {obs.height if obs else ''}"]
    if obs is not None and obs.error:
        notes.append(f"Station unreadable: {obs.error[:90]}")
    for key, pair in models.items():
        cell = (pair.get("buoy") or pair.get("station")).cell
        notes.append(f"{MODELS[key].label}: nearest water cell {cell.get('distance_km', '?')} km from the buoy; hourly, lead 1-12 h from the latest 00Z/12Z run")
    notes.append("Land anemometer vs open-water wind: a steady offset is expected.")
    # Two columns when long, split at the group heading nearest the middle.
    half = len(text)
    if len(text) > 14:
        headings = [k for k, s in enumerate(text) if k and not s.startswith("  ")]
        half = min(headings, key=lambda k: abs(k - len(text) / 2), default=len(text))
    ax_tx.text(0, 1, "\n".join(text[:half]), va="top", ha="left", fontsize=7.8, family="monospace", color=ink)
    if half < len(text):
        ax_tx.text(0.5, 1, "\n".join(text[half:]), va="top", ha="left", fontsize=7.8, family="monospace", color=ink)
    ax_tx.text(0, 0.02, "\n".join(notes), va="bottom", ha="left", fontsize=7.5, color=muted)

    first = times[0] if times else (obs.times[0] if obs and obs.times else None)
    last = times[-1] if times else (obs.times[-1] if obs and obs.times else None)
    window = f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC" if first and last else ""
    fig.suptitle(f"{station_name} buoy winds  ·  CDIP {station_id}  ·  {window}", x=0.06, ha="left", fontsize=12, color=ink, fontweight="bold")
    fig.subplots_adjust(top=0.94)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def bin_vectors(samples: list[tuple[datetime, float | None, float | None, bool]], width: timedelta):
    """Vector-average (time, speed, direction-from, good) samples into bins.

    Returns (bin centre, east, north, speed, all_questionable) per bin, where
    east and north are the components of the direction the wind blows toward.
    """
    bins: dict[int, list[float]] = {}
    step = width.total_seconds()
    for when, speed, direction, good in samples:
        if speed is None or direction is None:
            continue
        k = int(when.timestamp() // step)
        a = math.radians(direction)
        b = bins.setdefault(k, [0.0, 0.0, 0, False])
        b[0] += -speed * math.sin(a)
        b[1] += -speed * math.cos(a)
        b[2] += 1
        b[3] = b[3] or good
    out = []
    for k, (east, north, n, any_good) in sorted(bins.items()):
        e, nn = east / n, north / n
        out.append((datetime.fromtimestamp((k + 0.5) * step, tz=timezone.utc), e, nn, math.hypot(e, nn), not any_good))
    return out


def atomic_write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False, suffix=".tmp") as stream:
        stream.write(text)
    os.chmod(stream.name, 0o644)
    os.replace(stream.name, path)


def render_index(output_dir: Path, pages: list[dict[str, Any]]) -> str:
    """Landing page with each buoy's latest estimate and links to its charts."""
    def load(name: str) -> dict[str, Any]:
        path = output_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    latest, status = load("latest.json").get("stations", {}), load("status.json")
    cards = []
    for page in pages:
        sid, name = page["station_id"], page["station_name"]
        now = latest.get(sid) or {}
        error = status.get("stations", {}).get(sid, {}).get("error")
        if error:
            pill = '<span class="pill down">No data in latest run</span>'
        elif now.get("is_stale"):
            pill = '<span class="pill stale">Stale</span>'
        elif now:
            pill = '<span class="pill ok">Current</span>'
        else:
            pill = '<span class="pill stale">No estimate yet</span>'
        if now.get("estimated_u10_m_s") is not None:
            direction = now.get("estimated_wind_direction_deg_from")
            reading = f'<div class="big mono">{now["estimated_u10_m_s"]:.1f}<small>m/s</small></div>'
            if direction is not None:
                reading += (f'<div class="meta mono">from {direction:.0f}° '
                            f'{COMPASS_POINTS[round(direction / 22.5) % 16]} · '
                            f'direction confidence {html.escape(str(now.get("direction_confidence") or "n/a"))}</div>')
            reading += (f'<div class="meta mono">{html.escape(str(now.get("time_utc", "")))} · '
                        f'QC {html.escape(str(now.get("qc_status", "")))}</div>')
        else:
            reading = '<div class="big mono">—</div>'
        notice = ('<div class="notice">CDIP could not serve this buoy in the latest run; it may be offline '
                  'or under maintenance. Station observations are still charted.</div>') if error else ""
        speed = (page.get("stats") or {}).get("speed_all")
        valid = (f'<div class="meta">Buoy estimate vs {html.escape(page["obs_name"])}: bias {speed["bias"]:+.2f} m/s, '
                 f'RMSE {speed["rmse"]:.2f} m/s, N={speed["n"]}</div>') if speed else ""
        for row in (page.get("stats") or {}).get("table", []):
            if row["group"] == "Speed vs station" and row["series"] in MODELS and row["stats"]:
                s = row["stats"]
                valid += (f'<div class="meta">{html.escape(row["label"])} vs station: bias {s["bias"]:+.2f} m/s, '
                          f'RMSE {s["rmse"]:.2f} m/s, N={s["n"]}</div>')
        image = (f'<a href="{html.escape(page["png"])}"><img src="{html.escape(page["png"])}" '
                 f'alt="{html.escape(name)} wind chart" loading="lazy"></a>') if page.get("png") else ""
        png_link = f'<a href="{html.escape(page["png"])}">PNG figure</a>' if page.get("png") else ""
        cards.append(
            f'    <article class="card">\n'
            f'      <div><h2>{html.escape(name)}</h2><div class="id mono">CDIP {html.escape(sid)}</div></div>\n'
            f'      {pill}\n      {reading}\n      {notice}{valid}\n      {image}\n'
            f'      <div class="links"><a href="{html.escape(page["html"])}">Interactive chart</a>{png_link}</div>\n'
            f'    </article>'
        )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        INDEX_TEMPLATE.read_text(encoding="utf-8")
        .replace("__CARDS__", "\n".join(cards))
        .replace("__GEN__", generated)
        .replace("__RUN__", html.escape(str(status.get("run_at_utc", "unknown"))))
    )


def publish(output_dir: Path, web_dir: Path, names: list[str]) -> list[str]:
    """Copy pages, figures, and data products into a web root, atomically."""
    web_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in [*names, *PUBLISHED_DATA]:
        source = output_dir / name
        if not source.exists():
            continue
        with tempfile.NamedTemporaryFile(dir=web_dir, delete=False, suffix=".tmp") as stream:
            temporary = Path(stream.name)
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, web_dir / name)
        copied.append(name)
    return copied


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
    parser.add_argument("--no-model", action="store_true", help="Skip the WRF model comparison.")
    parser.add_argument(
        "--web-dir",
        type=Path,
        default=Path(os.environ["WINDPROXY_WEB_DIR"]) if os.environ.get("WINDPROXY_WEB_DIR") else None,
        help="Also copy pages, figures, and data here, e.g. an NGINX root. Default: $WINDPROXY_WEB_DIR.",
    )
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
    pages: list[dict[str, Any]] = []
    for station_id in args.station_ids or list(VALIDATION_STATIONS):
        rows, station_name = load_rows(args.input, station_id, args.days or None)
        obs = None
        start, end = plot_window(rows, args.days or None)
        if not args.no_validation:
            obs = fetch_observations(station_id, start, end)
        models = {} if args.no_model else load_models(station_id, start, end, output_dir / "model")
        has_obs = obs is not None and not obs.error and len(obs.times) >= 2
        if len(rows) < 2 and not has_obs:
            print(f"{station_name}: nothing to plot (need 2 buoy records or station observations).", file=sys.stderr)
            failures += 1
            continue
        matched, stats = validate(rows, obs, models)
        notices = station_notices(args.status, station_id, obs)
        html_path = output_dir / f"wind_plot_{station_id}.html"
        png_path = output_dir / f"wind_plot_{station_id}.png"
        written = []
        if png_ok:
            # Render to a temporary name so a failed figure never replaces a good one.
            temporary = png_path.with_suffix(".tmp.png")
            render_png(temporary, rows, station_id, station_name, obs, matched, stats, models)
            os.chmod(temporary, 0o644)
            os.replace(temporary, png_path)
            written.append(png_path.name)
        page = render(rows, station_id, station_name, source, notices, obs, matched, stats,
                      png_name=png_path.name if png_ok else None, models=models)
        atomic_write_text(html_path, page)
        written.insert(0, html_path.name)
        pages.append({"station_id": station_id, "station_name": station_name, "html": html_path.name,
                      "png": png_path.name if png_ok else None, "stats": stats,
                      "obs_name": obs.name if obs else ""})
        obs_note = "no station" if obs is None else (f"station error: {obs.error}" if obs.error else f"{len(obs.times)} station readings")
        speed = stats.get("speed_all")
        stat_note = f", speed bias {speed['bias']:+.2f} m/s RMSE {speed['rmse']:.2f}" if speed else ""
        for row in stats.get("table", []):
            if row["group"] == "Speed vs station" and row["series"] in MODELS and row["stats"]:
                stat_note += f", {row['label']} RMSE {row['stats']['rmse']:.2f}"
        print(f"{station_name}: {len(rows)} buoy records, {obs_note}{stat_note} -> {', '.join(written)} in {output_dir}")

    if pages:
        atomic_write_text(output_dir / "index.html", render_index(output_dir, pages))
        if args.web_dir:
            names = ["index.html"] + [p["html"] for p in pages] + [p["png"] for p in pages if p["png"]]
            copied = publish(output_dir, args.web_dir, names)
            print(f"Published {len(copied)} files to {args.web_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
