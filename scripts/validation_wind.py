"""Observed wind from CARICOOS WindNet stations, for validating buoy estimates.

Each CDIP buoy is paired with the nearest CARICOOS meteorological station. The
stations are read over OPeNDAP's plain-text ``.ascii`` interface with the
standard library only: the Arecibo file trips netCDF4's DAP client ("NC_UNLIMITED
in the wrong index"), and plain HTTP avoids a new dependency for everyone else.

The observations are land-based anemometer readings at the station's own height,
not neutral 10 m wind over open water, so a systematic offset against the buoy
estimate is expected; the comparison shows agreement in timing and trend as much
as in absolute level.
"""

from __future__ import annotations

import math
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

THREDDS_DAP = "https://dm1.caricoos.org/thredds/dodsC"
USER_AGENT = "WindProxy validation (https://github.com/caose-lab-org/WindProxy)"
# QARTOD: 1 pass, 2 not evaluated, 3 suspect, 4 fail, 9 missing. Validation
# should not lean on readings the operator doubts, so suspect is dropped too.
QARTOD_KEEP = {1, 2}
# CDIP waveTime marks the start of the ~30 minute buoy sample.
MATCH_WINDOW = timedelta(minutes=30)
TO_M_S = {
    "m/s": 1.0, "m s-1": 1.0, "meters per second": 1.0,
    "miles per hour": 0.44704, "mph": 0.44704,
    "knots": 0.514444, "kt": 0.514444, "kts": 0.514444,
}

VALIDATION_STATIONS: dict[str, dict[str, Any]] = {
    "249p1": {
        "station_id": "AROP4",
        "name": "Arecibo AROP4",
        "path": "content/WindNet/Arecibo/arop4_rt.nc",
        "speed": "wind_speed",
        "direction": "wind_direction",
        "speed_qc": "wind_speed_qc",
        "direction_qc": "wind_direction_qc",
        "has_station_dim": True,
        "height": "anemometer 12 m above site elevation",
    },
    "181p1": {
        "station_id": "E9889_TPR",
        "name": "Tres Palmas, Rincón",
        "path": "content/WindNet/Rincon/Tres_palmas/e9889_tpr_realtime.nc",
        "speed": "AvrgWS",
        "direction": "DirWS",
        "speed_qc": None,
        "direction_qc": None,
        "has_station_dim": False,
        "height": "Davis anemometer, height above ground not documented",
    },
}


@dataclass
class Observations:
    station_id: str
    name: str
    height: str
    source_url: str
    times: list[datetime] = field(default_factory=list)
    speed_m_s: list[float | None] = field(default_factory=list)
    direction_deg: list[float | None] = field(default_factory=list)
    error: str | None = None


def http_text(url: str, timeout: float = 90.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def variable_attributes(das: str, name: str) -> dict[str, str]:
    match = re.search(rf"^\s*{re.escape(name)} \{{(.*?)^\s*\}}", das, re.M | re.S)
    if not match:
        return {}
    return dict(re.findall(r'^\s*\w+ (\w+) "?(.*?)"?;\s*$', match.group(1), re.M))


def parse_time_units(units: str) -> tuple[float, datetime]:
    match = re.match(r"\s*(days|hours|minutes|seconds) since (\d{4}-\d{2}-\d{2})[ T]?(\d{1,2}:\d{2}(?::\d{2})?)?", units)
    if not match:
        raise ValueError(f"unrecognised time units {units!r}")
    step = {"days": 86400.0, "hours": 3600.0, "minutes": 60.0, "seconds": 1.0}[match.group(1)]
    clock = match.group(3) or "00:00:00"
    if clock.count(":") == 1:
        clock += ":00"
    # WindNet files state UTC ("UTC" or "+0:00"); anything else would need handling here.
    epoch = datetime.fromisoformat(f"{match.group(2)}T{clock.zfill(8)}").replace(tzinfo=timezone.utc)
    return step, epoch


def parse_ascii(text: str) -> dict[str, list[float]]:
    """Parse an OPeNDAP .ascii response into {variable: values}."""
    body = re.split(r"^-{10,}\s*$", text, maxsplit=1, flags=re.M)[-1]
    arrays: dict[str, list[float]] = {}
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        name = lines[0].split("[", 1)[0].split(".")[-1].strip()
        values: list[float] = []
        for line in lines[1:]:
            line = re.sub(r"^\s*\[\d+\],\s*", "", line)
            for item in line.split(","):
                item = item.strip()
                if not item:
                    continue
                try:
                    values.append(float(item))
                except ValueError:
                    values.append(float("nan"))
        arrays[name] = values
    return arrays


def clean(value: float) -> float | None:
    return None if not math.isfinite(value) or value <= -900 else value


def fetch_observations(buoy_station_id: str, start: datetime, end: datetime) -> Observations | None:
    config = VALIDATION_STATIONS.get(buoy_station_id)
    if config is None:
        return None
    base = f"{THREDDS_DAP}/{config['path']}"
    result = Observations(config["station_id"], config["name"], config["height"], base)
    try:
        dds = http_text(f"{base}.dds")
        length = re.search(r"time\[time = (\d+)\]", dds)
        if not length:
            raise ValueError("no time dimension in .dds")
        count = int(length.group(1))
        das = http_text(f"{base}.das")
        step, epoch = parse_time_units(variable_attributes(das, "time").get("units", ""))
        units = variable_attributes(das, config["speed"]).get("units", "m/s").strip().lower()
        if units not in TO_M_S:
            raise ValueError(f"unsupported wind speed units {units!r}")
        factor = TO_M_S[units]

        # The files are appended in time order, so only the tail is needed. Five
        # minutes is the finest cadence among the stations; the margin covers gaps.
        span_minutes = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
        first = max(0, count - int(span_minutes / 5) - 200)
        slab = f"[{first}:1:{count - 1}]"
        slab_2d = f"[0:0]{slab}" if config["has_station_dim"] else slab
        names = [config["speed"], config["direction"], config["speed_qc"], config["direction_qc"]]
        query = ",".join(["time" + slab] + [f"{n}{slab_2d}" for n in names if n])
        arrays = parse_ascii(http_text(f"{base}.ascii?{query}"))
    except Exception as error:
        result.error = f"{type(error).__name__}: {error}"
        return result

    speed_qc = arrays.get(config["speed_qc"] or "", [])
    direction_qc = arrays.get(config["direction_qc"] or "", [])
    for index, offset in enumerate(arrays.get("time", [])):
        if not math.isfinite(offset):
            continue
        # Float day offsets carry microsecond noise; whole seconds are exact enough.
        when = epoch + timedelta(seconds=round(offset * step))
        if not start <= when <= end:
            continue
        speed = clean(arrays[config["speed"]][index])
        direction = clean(arrays[config["direction"]][index])
        if speed_qc and math.isfinite(speed_qc[index]) and int(speed_qc[index]) not in QARTOD_KEEP:
            speed = None
        if direction_qc and math.isfinite(direction_qc[index]) and int(direction_qc[index]) not in QARTOD_KEEP:
            direction = None
        if speed is not None and speed < 0:
            speed = None
        result.times.append(when)
        result.speed_m_s.append(None if speed is None else speed * factor)
        result.direction_deg.append(direction)
    return result


def match_to_buoy(buoy_times: list[datetime], obs: Observations) -> list[tuple[float | None, float | None]]:
    """Average station readings over each buoy sample window.

    Speed is the scalar mean; direction is the vector mean of unit bearings, so
    readings either side of north do not average to south.
    """
    matched = []
    j = 0
    for start in buoy_times:
        stop = start + MATCH_WINDOW
        while j < len(obs.times) and obs.times[j] < start:
            j += 1
        speeds, sines, cosines = [], [], []
        k = j
        while k < len(obs.times) and obs.times[k] < stop:
            if obs.speed_m_s[k] is not None:
                speeds.append(obs.speed_m_s[k])
            if obs.direction_deg[k] is not None:
                radians = math.radians(obs.direction_deg[k])
                sines.append(math.sin(radians))
                cosines.append(math.cos(radians))
            k += 1
        speed = sum(speeds) / len(speeds) if speeds else None
        direction = None
        if sines:
            s, c = sum(sines) / len(sines), sum(cosines) / len(cosines)
            if math.hypot(s, c) > 1e-9:
                direction = math.degrees(math.atan2(s, c)) % 360.0
        matched.append((speed, direction))
    return matched


def angle_difference(a: float, b: float) -> float:
    """Signed a - b wrapped to [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def comparison_stats(pairs: list[tuple[float, float]], circular: bool = False) -> dict[str, float | None] | None:
    """Bias (estimate minus observed), RMSE, and, for speed, Pearson r."""
    if len(pairs) < 2:
        return None
    if circular:
        diffs = [angle_difference(e, o) for e, o in pairs]
    else:
        diffs = [e - o for e, o in pairs]
    n = len(diffs)
    stats = {
        "n": n,
        "bias": sum(diffs) / n,
        "rmse": math.sqrt(sum(d * d for d in diffs) / n),
    }
    if not circular:
        est = [e for e, _ in pairs]
        obs = [o for _, o in pairs]
        me, mo = sum(est) / n, sum(obs) / n
        se = math.sqrt(sum((x - me) ** 2 for x in est))
        so = math.sqrt(sum((x - mo) ** 2 for x in obs))
        stats["r"] = sum((x - me) * (y - mo) for x, y in zip(est, obs)) / (se * so) if se and so else None
    return stats
