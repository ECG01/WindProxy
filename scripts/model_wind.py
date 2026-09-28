"""10 m wind from the CARICOOS WRF-NMM forecasts at each buoy and station.

Both CARICOOS WRF domains (1 km, and the 2 km d02 nest of the 6/2 km run) are
published on THREDDS as one wrfout file per forecast hour, twice a day at 00Z
and 12Z. For validation each hour is taken from the freshest run that covers it
at a lead of 1 to 12 hours, falling back to older runs when a file is missing,
which gives a continuous short-range forecast series.

Only the few grid cells around the sites are requested, over OPeNDAP's plain
text interface with the standard library. Values already fetched are cached in
a CSV, so a polling run only downloads forecast hours it has not seen.

Each buoy uses its nearest water cell; each land station uses its nearest cell
of any kind. WRF-NMM winds are relative to its rotated grid, so they are turned
to earth-relative using the grid orientation measured from GLAT/GLON.
"""

from __future__ import annotations

import csv
import fcntl
import json
import math
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from validation_wind import THREDDS_DAP, USER_AGENT, VALIDATION_STATIONS, http_text, parse_ascii

THREDDS_CATALOG = "https://dm1.caricoos.org/thredds/catalog"
CATALOG_NS = {"c": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}
FILE_TIME = re.compile(r"wrfout_d\d\d_(\d{4}-\d{2}-\d{2})_(\d{2})[:_-](\d{2})[:_-](\d{2})\.nc$")
CYCLE = timedelta(hours=12)
# Leads 1-12 h from the freshest run, then up to two older runs as fallback.
FALLBACK_RUNS = 2
CACHE_FIELDS = ["model", "site", "valid_time", "run", "lead_hours", "u_m_s", "v_m_s", "speed_m_s", "direction_deg_from"]


@dataclass(frozen=True)
class ModelSource:
    key: str
    label: str
    catalog_path: str
    run_subpath: str


MODELS: dict[str, ModelSource] = {
    "wrf1km": ModelSource("wrf1km", "WRF 1 km", "content/wrf_archive/wrf_nmm_1km", ""),
    "wrf2km": ModelSource("wrf2km", "WRF 2 km", "content/wrf_archive/wrf_nmm_62km", "d02"),
}


def sites() -> dict[str, tuple[float, float, bool]]:
    """{site: (lat, lon, water_only)}; site is '<buoy>' or '<buoy>:station'."""
    out = {}
    for buoy, config in VALIDATION_STATIONS.items():
        out[buoy] = (config["buoy_latitude"], config["buoy_longitude"], True)
        out[f"{buoy}:station"] = (config["latitude"], config["longitude"], False)
    return out


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return 111.2 * math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(math.radians(lat1)))


def catalog_entries(relative_path: str) -> ET.Element:
    request = urllib.request.Request(f"{THREDDS_CATALOG}/{relative_path}/catalog.xml", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return ET.fromstring(response.read())


def run_files(source: ModelSource, run: str) -> dict[datetime, str]:
    """{valid_time: OPeNDAP urlPath} for one run, or {} if it is not published."""
    path = f"{source.catalog_path}/{run}" + (f"/{source.run_subpath}" if source.run_subpath else "")
    try:
        root = catalog_entries(path)
    except Exception:
        return {}
    files = {}
    for dataset in root.findall(".//c:dataset", CATALOG_NS):
        url_path, name = dataset.get("urlPath"), dataset.get("name", "")
        match = FILE_TIME.search(name)
        if url_path and match:
            day, hour, minute, second = match.groups()
            files[datetime.fromisoformat(f"{day}T{hour}:{minute}:{second}+00:00")] = url_path
    return files


def locate(source: ModelSource, cache_dir: Path) -> dict[str, Any]:
    """Grid indices and grid-to-earth rotation for every site, cached per model."""
    path = cache_dir / f"{source.key}_grid.json"
    wanted = sites()
    if path.exists():
        grid = json.loads(path.read_text(encoding="utf-8"))
        if set(grid["sites"]) == set(wanted) and all(
            grid["sites"][s]["target"] == list(wanted[s][:2]) for s in wanted
        ):
            return grid

    # Any published file will do; the grid does not change between runs.
    now = datetime.now(timezone.utc)
    files: dict[datetime, str] = {}
    for back in range(6):
        run_time = cycle_start(now) - back * CYCLE
        files = run_files(source, run_time.strftime("%Y%m%d%H"))
        if files:
            break
    if not files:
        raise RuntimeError(f"no recent {source.label} run found on THREDDS")
    url = f"{THREDDS_DAP}/{files[min(files)]}"
    dds = http_text(f"{url}.dds")
    ny, nx = map(int, re.search(r"GLAT\[Time = 1\]\[south_north = (\d+)\]\[west_east = (\d+)\]", dds).groups())
    fields = parse_ascii(http_text(f"{url}.ascii?GLAT,GLON,SST"))
    lat = [math.degrees(v) for v in fields["GLAT"]]
    lon = [math.degrees(v) for v in fields["GLON"]]
    water = [v > 0 for v in fields["SST"]]  # SST is 0 over land cells

    located = {}
    for site, (target_lat, target_lon, water_only) in wanted.items():
        candidates = [k for k in range(ny * nx) if water[k]] if water_only else range(ny * nx)
        best = min(candidates, key=lambda k: distance_km(target_lat, target_lon, lat[k], lon[k]))
        j, i = divmod(best, nx)
        # Direction of the grid's x axis from two cells along the row.
        left, right = j * nx + max(i - 1, 0), j * nx + min(i + 1, nx - 1)
        angle = math.degrees(math.atan2(
            lat[right] - lat[left],
            (lon[right] - lon[left]) * math.cos(math.radians(lat[best])),
        ))
        located[site] = {
            "target": [target_lat, target_lon],
            "j": j, "i": i,
            "latitude": round(lat[best], 5), "longitude": round(lon[best], 5),
            "distance_km": round(distance_km(target_lat, target_lon, lat[best], lon[best]), 2),
            "water": water[best],
            "rotation_deg": round(angle, 4),
        }
    grid = {"model": source.key, "ny": ny, "nx": nx, "sites": located}
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(grid, indent=2) + "\n")
    return grid


def cycle_start(when: datetime) -> datetime:
    return when.replace(hour=(when.hour // 12) * 12, minute=0, second=0, microsecond=0)


def plan_hours(hours: list[datetime], source: ModelSource, catalogs: dict[str, dict[datetime, str]]) -> dict[datetime, tuple[str, int, str]]:
    """{valid_time: (run, lead_hours, urlPath)} from the freshest run covering each hour."""
    plan = {}
    for valid in hours:
        first = cycle_start(valid - timedelta(hours=1))  # lead >= 1 h
        for back in range(FALLBACK_RUNS + 1):
            run_time = first - back * CYCLE
            run = run_time.strftime("%Y%m%d%H")
            if run not in catalogs:
                catalogs[run] = run_files(source, run)
            url_path = catalogs[run].get(valid)
            if url_path:
                plan[valid] = (run, int((valid - run_time).total_seconds() // 3600), url_path)
                break
    return plan


def fetch_hour(url_path: str, grid: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Earth-relative (u, v) at every site from one wrfout file."""
    js = [s["j"] for s in grid["sites"].values()]
    is_ = [s["i"] for s in grid["sites"].values()]
    j0, j1, i0, i1 = min(js), max(js), min(is_), max(is_)
    box = f"[0:0][{j0}:{j1}][{i0}:{i1}]"
    fields = parse_ascii(http_text(f"{THREDDS_DAP}/{url_path}.ascii?U10{box},V10{box}"))
    width = i1 - i0 + 1
    out = {}
    for site, cell in grid["sites"].items():
        k = (cell["j"] - j0) * width + (cell["i"] - i0)
        u, v = fields["U10"][k], fields["V10"][k]
        a = math.radians(cell["rotation_deg"])
        out[site] = (u * math.cos(a) - v * math.sin(a), u * math.sin(a) + v * math.cos(a))
    return out


def atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False, suffix=".tmp") as stream:
        stream.write(text)
    os.replace(stream.name, path)


def read_cache(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as stream:
        return {(row["site"], row["valid_time"]): row for row in csv.DictReader(stream)}


def write_cache(path: Path, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", newline="", delete=False, suffix=".tmp") as stream:
        writer = csv.DictWriter(stream, fieldnames=CACHE_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=lambda r: (r["valid_time"], r["site"])))
    os.replace(stream.name, path)


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def update(source: ModelSource, start: datetime, end: datetime, cache_dir: Path,
           max_files: int = 120, workers: int = 4) -> str:
    """Fetch missing hours in [start, end] into the cache. Returns a short status line."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = (cache_dir / f".{source.key}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return f"{source.label}: another run is updating the cache"
    try:
        grid = locate(source, cache_dir)
        path = cache_dir / f"{source.key}_points.csv"
        cache = read_cache(path)
        first = start.replace(minute=0, second=0, microsecond=0)
        hours = []
        valid = first
        while valid <= end:
            if any((site, iso(valid)) not in cache for site in grid["sites"]):
                hours.append(valid)
            valid += timedelta(hours=1)
        # Newest first, so a capped catch-up always covers the recent past.
        hours.sort(reverse=True)
        plan = plan_hours(hours, source, {})
        todo = sorted(plan.items(), reverse=True)[:max_files]

        def work(item):
            valid, (run, lead, url_path) = item
            try:
                return valid, run, lead, fetch_hour(url_path, grid)
            except Exception:
                return valid, run, lead, None

        fetched = failed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for valid, run, lead, values in pool.map(work, todo):
                if values is None:
                    failed += 1
                    continue
                fetched += 1
                for site, (u, v) in values.items():
                    cache[(site, iso(valid))] = {
                        "model": source.key, "site": site, "valid_time": iso(valid), "run": run,
                        "lead_hours": lead, "u_m_s": round(u, 3), "v_m_s": round(v, 3),
                        "speed_m_s": round(math.hypot(u, v), 3),
                        # Meteorological convention: the bearing the wind blows from.
                        "direction_deg_from": round((270.0 - math.degrees(math.atan2(v, u))) % 360.0, 1),
                    }
        if fetched:
            write_cache(path, cache)
        pending = len(plan) - len(todo)
        return (f"{source.label}: {fetched} new hours" + (f", {failed} failed" if failed else "")
                + (f", {pending} left for later runs" if pending else "")
                + (f", {len(hours) - len(plan)} hours not published" if len(hours) > len(plan) else ""))
    finally:
        lock.close()


@dataclass
class ModelSeries:
    key: str
    label: str
    times: list[datetime]
    u: list[float]
    v: list[float]
    cell: dict[str, Any]

    def at(self, when: datetime) -> tuple[float, float] | None:
        """(speed, direction_from) linearly interpolated in u and v; None outside the series or across gaps."""
        if not self.times or when < self.times[0] or when > self.times[-1]:
            return None
        lo, hi = 0, len(self.times) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.times[mid] <= when:
                lo = mid
            else:
                hi = mid
        t0, t1 = self.times[lo], self.times[hi]
        if t1 - t0 > timedelta(hours=1, minutes=5):
            return None
        w = 0.0 if t1 == t0 else (when - t0).total_seconds() / (t1 - t0).total_seconds()
        u = self.u[lo] + w * (self.u[hi] - self.u[lo])
        v = self.v[lo] + w * (self.v[hi] - self.v[lo])
        return math.hypot(u, v), (270.0 - math.degrees(math.atan2(v, u))) % 360.0


def load_series(source: ModelSource, site: str, start: datetime, end: datetime, cache_dir: Path) -> ModelSeries | None:
    path = cache_dir / f"{source.key}_points.csv"
    grid_path = cache_dir / f"{source.key}_grid.json"
    if not path.exists() or not grid_path.exists():
        return None
    cell = json.loads(grid_path.read_text(encoding="utf-8"))["sites"].get(site, {})
    rows = sorted(
        (r for (s, _), r in read_cache(path).items() if s == site),
        key=lambda r: r["valid_time"],
    )
    series = ModelSeries(source.key, source.label, [], [], [], cell)
    for row in rows:
        when = datetime.fromisoformat(row["valid_time"].replace("Z", "+00:00"))
        if start <= when <= end:
            series.times.append(when)
            series.u.append(float(row["u_m_s"]))
            series.v.append(float(row["v_m_s"]))
    return series if series.times else None
