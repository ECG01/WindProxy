#!/usr/bin/env python3
"""Operational CDIP spectrum-to-wind processor for Puerto Rico buoys.

The command polls CDIP's real-time OPeNDAP datasets, estimates neutral 10 m
wind speed for previously unseen spectra, and atomically updates CSV and JSON
products. It is safe to invoke more often than the buoys report observations.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import xarray as xr
from scipy.optimize import brentq


VERSION = "1.1.0"
CDIP_REALTIME = "https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/realtime"
CDIP_ARCHIVE = "https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/archive"
BETA = 0.012
IP = 2.5
GRAVITY = 9.81
TAIL_MAX_HZ = 0.5
MIN_TAIL_BINS = 3
TAIL_CV_WARNING = 0.5
GOOD_SOURCE_FLAGS = {1, 2}
GOOD_FREQUENCY_FLAGS = {1, 2}
DEFAULT_HISTORY_START = "2026-01-01T00:00:00Z"

# Mudd et al. (2024) "Wind velocity estimates from wave observing platforms",
# Coastal Engineering Journal 66(3), 479-491. doi:10.1080/21664250.2024.2321660
#
# Section 2.1.1: candidate equilibrium bands are contiguous segments above 2 fp
# spanning 0.14-0.34 Hz (15-35 indices on the 0.01 Hz Datawell MkIII grid); the
# band whose E(f) f^4 regression slope is closest to zero is selected.
ADAPTIVE_WIDTH_HZ = (0.14, 0.34)
# fmax in Mudd et al. is the highest frequency the platform resolves, 0.58 Hz for
# the Datawell MkIII of CDIP 185. CDIP realtime publishes bands to 1.0 Hz but
# flags everything above the hull response limit, so both bounds are applied.
ADAPTIVE_MAX_HZ = 0.58
# Section 2.1.3: spectra whose log-log slope over the selected band departs from
# the Phillips f^-4 equilibrium do not support a reliable wind proxy.
EQUILIBRIUM_SLOPE_RANGE = (-4.4, -3.6)
# Section 2.1.2 and Voermans et al. (2020) equation 14: the wind direction is the
# uniformly weighted circular mean of per-band mean wave direction over the same
# frequency range that produced the speed.
MIN_DIRECTION_BINS = 3
# Below this resultant length the band's directions have cancelled and their
# circular mean carries no information; floating point never yields exactly 0.
MIN_RESULTANT_LENGTH = 1e-9
# Table 2: direction RMSE is 13.2 deg above 7 m/s but 56.2 deg below it.
DIRECTION_CONFIDENCE_U10 = 7.0
# Section 5: the method is only dependable between 3 and 12 m/s.
RELIABLE_U10_RANGE = (3.0, 12.0)

STATIONS = {
    "arecibo": {"station_id": "249p1", "station_name": "Arecibo"},
    "rincon": {"station_id": "181p1", "station_name": "Rincon"},
}

CSV_FIELDS = [
    "station_id",
    "station_name",
    "time_utc",
    "estimated_u10_m_s",
    "estimated_wind_direction_deg_from",
    "friction_velocity_m_s",
    "neutral_drag_coefficient",
    "direction_resultant_length",
    "direction_circular_spread_deg",
    "mean_band_directional_spread_deg",
    "direction_bin_count",
    "direction_confidence",
    "wind_reliability",
    "equilibrium_log_slope",
    "band_method",
    "significant_wave_height_m",
    "peak_period_s",
    "peak_frequency_hz",
    "tail_min_hz",
    "tail_max_hz",
    "tail_bin_count",
    "mean_spectrum_f4_m2_hz3",
    "tail_coefficient_of_variation",
    "source_wave_qc_flag",
    "qc_status",
    "qc_reasons",
    "source_url",
    "estimator_version",
    "processed_at_utc",
]

JSON_FLOAT_FIELDS = {
    "estimated_u10_m_s",
    "friction_velocity_m_s",
    "neutral_drag_coefficient",
    "significant_wave_height_m",
    "peak_period_s",
    "peak_frequency_hz",
    "tail_min_hz",
    "tail_max_hz",
    "mean_spectrum_f4_m2_hz3",
    "tail_coefficient_of_variation",
    "estimated_wind_direction_deg_from",
    "direction_resultant_length",
    "direction_circular_spread_deg",
    "mean_band_directional_spread_deg",
    "equilibrium_log_slope",
}
JSON_INT_FIELDS = {"tail_bin_count", "source_wave_qc_flag", "direction_bin_count"}


@dataclass(frozen=True, eq=False)
class SpectralEstimate:
    mean_spectrum_f4: float
    friction_velocity: float
    peak_frequency: float
    tail_min_frequency: float
    tail_max_frequency: float
    tail_bin_count: int
    tail_cv: float
    equilibrium_log_slope: float
    band_method: str
    band_mask: np.ndarray


@dataclass(frozen=True)
class DirectionEstimate:
    """Wind direction inferred from the equilibrium range of the wave spectrum."""

    direction_from_deg: float
    resultant_length: float
    circular_spread_deg: float
    mean_band_spread_deg: float | None
    bin_count: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | np.datetime64) -> str:
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            raise ValueError("missing observation time")
        return np.datetime_as_string(value.astype("datetime64[s]"), unit="s") + "Z"
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def realtime_url(base_url: str, station_id: str) -> str:
    return f"{base_url.rstrip('/')}/{station_id}_rt.nc"


def archive_url(archive_base: str, station_id: str) -> str:
    return f"{archive_base.rstrip('/')}/{station_id}/{station_id}_historic.nc"


def first_present(dataset: xr.Dataset, names: Iterable[str], required: bool = True) -> str | None:
    for name in names:
        if name in dataset.variables:
            return name
    if required:
        raise KeyError(f"Missing required CDIP variable; tried {', '.join(names)}")
    return None


def equilibrium_log_slope(frequency: np.ndarray, energy: np.ndarray, mask: np.ndarray) -> float:
    """Log-log slope of E(f) over the selected band; -4 in a Phillips equilibrium."""
    usable = mask & (energy > 0) & (frequency > 0)
    if np.count_nonzero(usable) < 2:
        return float("nan")
    slope = np.polyfit(np.log(frequency[usable]), np.log(energy[usable]), 1)[0]
    return float(slope)


def select_adaptive_band(
    frequency: np.ndarray,
    spectrum_f4: np.ndarray,
    candidate: np.ndarray,
) -> np.ndarray:
    """Choose the contiguous band whose E(f) f^4 regression slope is closest to zero.

    Implements the fitting method of Mudd et al. (2024) Section 2.1.1. Candidate
    bands are every contiguous run of usable bins spanning ADAPTIVE_WIDTH_HZ. The
    per-segment least-squares slopes are evaluated in closed form from prefix
    sums so that a full history rebuild stays inexpensive.
    """
    indices = np.flatnonzero(candidate)
    if indices.size < MIN_TAIL_BINS:
        raise ValueError(f"equilibrium tail contains {indices.size} usable bins")

    x = frequency[indices]
    y = spectrum_f4[indices]
    # Prefix sums so that any segment's regression slope is O(1) to evaluate.
    cumulative = [np.concatenate(([0.0], np.cumsum(v))) for v in (x, y, x * x, x * y)]
    sum_x, sum_y, sum_xx, sum_xy = cumulative

    starts, ends = np.triu_indices(indices.size, k=MIN_TAIL_BINS - 1)
    width = x[ends] - x[starts]
    feasible = (width >= ADAPTIVE_WIDTH_HZ[0]) & (width <= ADAPTIVE_WIDTH_HZ[1])
    if not feasible.any():
        raise ValueError(
            f"no contiguous band spans {ADAPTIVE_WIDTH_HZ[0]}-{ADAPTIVE_WIDTH_HZ[1]} Hz"
        )
    starts, ends = starts[feasible], ends[feasible]

    count = (ends - starts + 1).astype(float)
    take = lambda c: c[ends + 1] - c[starts]  # noqa: E731 - segment sum from prefix sums
    seg_x, seg_y, seg_xx, seg_xy = take(sum_x), take(sum_y), take(sum_xx), take(sum_xy)
    denominator = count * seg_xx - seg_x**2
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(denominator > 0, (count * seg_xy - seg_x * seg_y) / denominator, np.inf)

    best = int(np.argmin(np.abs(slope)))
    chosen = np.zeros_like(candidate)
    chosen[indices[starts[best]] : indices[ends[best]] + 1] = True
    return chosen & candidate


def estimate_friction_velocity(
    frequency_hz: np.ndarray,
    energy_density: np.ndarray,
    peak_period_s: float,
    *,
    band_method: str = "fixed",
    usable_bins: np.ndarray | None = None,
) -> SpectralEstimate:
    """Estimate air-side friction velocity from the equilibrium spectral tail.

    With ``band_method="fixed"`` the band is 2 fp to TAIL_MAX_HZ. With
    ``band_method="adaptive"`` it is chosen by the Mudd et al. (2024) fitting
    method. ``usable_bins`` optionally excludes frequency bands that the source
    flags as unreliable, such as those above a platform's hull response limit.
    """
    frequency = np.asarray(frequency_hz, dtype=float)
    energy = np.ma.asarray(energy_density).filled(np.nan).astype(float)
    if frequency.ndim != 1 or energy.ndim != 1 or frequency.shape != energy.shape:
        raise ValueError("frequency and energy must be equal-length one-dimensional arrays")
    if not math.isfinite(peak_period_s) or peak_period_s <= 0:
        raise ValueError("peak period must be positive and finite")

    if band_method not in {"fixed", "adaptive"}:
        raise ValueError(f"unknown band method {band_method!r}")

    peak_frequency = 1.0 / peak_period_s
    tail_min = 2.0 * peak_frequency
    spectrum_f4 = energy * frequency**4
    ceiling = TAIL_MAX_HZ if band_method == "fixed" else ADAPTIVE_MAX_HZ
    mask = (
        np.isfinite(frequency)
        & np.isfinite(spectrum_f4)
        & (energy >= 0)
        & (frequency >= tail_min)
        & (frequency <= ceiling)
    )
    if usable_bins is not None:
        mask &= np.asarray(usable_bins, dtype=bool)
    if band_method == "adaptive":
        mask = select_adaptive_band(frequency, spectrum_f4, mask)

    values = spectrum_f4[mask]
    if values.size < MIN_TAIL_BINS:
        raise ValueError(
            f"equilibrium tail contains {values.size} bins; at least {MIN_TAIL_BINS} are required"
        )

    mean_f4 = float(np.mean(values))
    if not math.isfinite(mean_f4) or mean_f4 <= 0:
        raise ValueError("mean equilibrium-tail level must be positive and finite")
    tail_cv = float(np.std(values, ddof=0) / mean_f4)
    ustar = mean_f4 * (2.0 * np.pi) ** 3 / (4.0 * BETA * IP * GRAVITY)
    if not math.isfinite(ustar) or ustar <= 0:
        raise ValueError("estimated friction velocity is not positive and finite")

    band_frequencies = frequency[mask]
    return SpectralEstimate(
        mean_spectrum_f4=mean_f4,
        friction_velocity=float(ustar),
        peak_frequency=float(peak_frequency),
        tail_min_frequency=float(band_frequencies[0]),
        tail_max_frequency=float(band_frequencies[-1]),
        tail_bin_count=int(values.size),
        tail_cv=tail_cv,
        equilibrium_log_slope=equilibrium_log_slope(frequency, energy, mask),
        band_method=band_method,
        band_mask=mask,
    )


def estimate_wind_direction(
    mean_wave_direction_deg: np.ndarray,
    band_mask: np.ndarray,
    band_spread_deg: np.ndarray | None = None,
) -> DirectionEstimate:
    """Infer wind direction from mean wave direction over the equilibrium range.

    Momentum enters the wave field mainly at high frequency, so waves in the
    equilibrium range travel nearly with the wind. Mudd et al. (2024) Section
    2.1.2 averages the per-band mean wave direction across the same frequencies
    used for the speed; Voermans et al. (2020) equation 14 gives that average as
    a uniformly weighted circular mean.

    CDIP publishes ``waveMeanDirection`` as ``sea_surface_wave_from_direction``
    in degrees true, which is already the theta(f) of Voermans equation 13 and
    shares the meteorological "direction the wind blows from" convention. Note
    that CDIP's published a1/b1 are pre-rotated, so recomputing theta(f) as
    ``270 - atan2(b1, a1)`` from them does not reproduce waveMeanDirection.
    """
    directions = np.ma.asarray(mean_wave_direction_deg).filled(np.nan).astype(float)
    if directions.shape != band_mask.shape:
        raise ValueError("direction and band mask must have the same shape")

    usable = band_mask & np.isfinite(directions)
    count = int(np.count_nonzero(usable))
    if count < MIN_DIRECTION_BINS:
        raise ValueError(
            f"equilibrium band has {count} directional bins; at least {MIN_DIRECTION_BINS} are required"
        )

    angles = np.radians(directions[usable])
    sine, cosine = float(np.mean(np.sin(angles))), float(np.mean(np.cos(angles)))
    resultant = math.hypot(sine, cosine)
    if resultant < MIN_RESULTANT_LENGTH:
        raise ValueError("equilibrium-band wave directions cancel; no mean direction exists")

    direction = math.degrees(math.atan2(sine, cosine)) % 360.0
    if direction >= 360.0:
        # A hair below due north lands on 360.0 once the modulo rounds.
        direction = 0.0
    # Mardia's circular standard deviation; 0 when every band agrees.
    circular_spread = math.degrees(math.sqrt(-2.0 * math.log(min(resultant, 1.0))))

    mean_band_spread: float | None = None
    if band_spread_deg is not None:
        spreads = np.ma.asarray(band_spread_deg).filled(np.nan).astype(float)
        finite = usable & np.isfinite(spreads)
        if finite.any():
            mean_band_spread = float(np.mean(spreads[finite]))

    return DirectionEstimate(
        direction_from_deg=direction,
        resultant_length=resultant,
        circular_spread_deg=circular_spread,
        mean_band_spread_deg=mean_band_spread,
        bin_count=count,
    )


def inverse_coare_u10(ustar: float, significant_wave_height_m: float, peak_period_s: float) -> tuple[float, float]:
    """Invert COARE 3.6 for neutral 10 m wind speed and drag coefficient."""
    from pycoare import coare_36

    if significant_wave_height_m <= 0 or not math.isfinite(significant_wave_height_m):
        raise ValueError("significant wave height must be positive and finite")
    phase_speed = GRAVITY * peak_period_s / (2.0 * np.pi)

    def residual(u10: float) -> float:
        result = coare_36(u=[u10], zu=[10], zrf=[10], cp=[phase_speed], sigH=[significant_wave_height_m])
        return float(result.velocities.usr[0]) - ustar

    u10 = float(brentq(residual, 0.1, 50.0))
    result = coare_36(u=[u10], zu=[10], zrf=[10], cp=[phase_speed], sigH=[significant_wave_height_m])
    cd = float(result.transfer_coefficients.cdn_rf[0])
    return u10, cd


def read_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def last_times(rows: Iterable[dict[str, str]]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for row in rows:
        try:
            station_id = row["station_id"]
            timestamp = parse_utc(row["time_utc"])
        except (KeyError, TypeError, ValueError):
            continue
        latest[station_id] = max(timestamp, latest.get(station_id, timestamp))
    return latest


def scalar(value: Any) -> float:
    if np.ma.is_masked(value):
        return float("nan")
    return float(value)


def process_station(
    station: dict[str, str],
    cutoff: datetime,
    processed_at: str,
    source_url: str,
    band_method: str = "fixed",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    station_id = station["station_id"]
    records: list[dict[str, Any]] = []

    with xr.open_dataset(source_url, engine="netcdf4", decode_times=True) as dataset:
        time_name = first_present(dataset, ["waveTime", "time"])
        frequency_name = first_present(dataset, ["waveFrequency", "frequency_hz"])
        energy_name = first_present(dataset, ["waveEnergyDensity", "energy_density"])
        hs_name = first_present(dataset, ["waveHs", "hs"])
        tp_name = first_present(dataset, ["waveTp", "tp"])
        source_qc_name = first_present(dataset, ["waveFlagPrimary"], required=False)
        direction_name = first_present(
            dataset, ["waveMeanDirection", "mean_wave_direction"], required=False
        )
        spread_name = first_present(dataset, ["waveSpread"], required=False)
        frequency_qc_name = first_present(dataset, ["waveFrequencyFlagPrimary"], required=False)

        times = np.asarray(dataset[time_name].values).astype("datetime64[ns]")
        cutoff64 = np.datetime64(cutoff.replace(tzinfo=None), "ns")
        indices = np.flatnonzero((~np.isnat(times)) & (times > cutoff64))
        if indices.size == 0:
            return [], {"source_url": source_url, "available": 0, "processed": 0}

        variable_names = [energy_name, hs_name, tp_name]
        for optional in (source_qc_name, direction_name, spread_name):
            if optional:
                variable_names.append(optional)
        time_dim = dataset[time_name].dims[0]
        subset = dataset[variable_names].isel({time_dim: indices}).load()
        frequencies = np.asarray(dataset[frequency_name].values, dtype=float)

        # CDIP flags bands outside the hull response limit; excluding them keeps
        # the equilibrium fit off frequencies the buoy cannot resolve.
        if frequency_qc_name:
            frequency_flags = np.asarray(dataset[frequency_qc_name].values, dtype=float)
            usable_bins = np.isin(frequency_flags, list(GOOD_FREQUENCY_FLAGS))
        else:
            usable_bins = np.ones_like(frequencies, dtype=bool)

        for local_index, source_index in enumerate(indices):
            timestamp = iso_utc(times[source_index])
            hs = scalar(subset[hs_name].values[local_index])
            tp = scalar(subset[tp_name].values[local_index])
            source_qc = (
                scalar(subset[source_qc_name].values[local_index]) if source_qc_name else float("nan")
            )
            source_qc_int = int(source_qc) if math.isfinite(source_qc) else None
            reasons: list[str] = []
            status = "good"
            estimate: SpectralEstimate | None = None
            direction: DirectionEstimate | None = None
            u10 = cd = None

            if source_qc_int is not None and source_qc_int not in GOOD_SOURCE_FLAGS:
                reasons.append(f"source_wave_qc_{source_qc_int}")
                status = "rejected" if source_qc_int in {4, 9} else "questionable"

            try:
                if not math.isfinite(hs) or hs <= 0:
                    raise ValueError("invalid significant wave height")
                estimate = estimate_friction_velocity(
                    frequencies,
                    subset[energy_name].values[local_index, :],
                    tp,
                    band_method=band_method,
                    usable_bins=usable_bins,
                )
                if estimate.tail_cv > TAIL_CV_WARNING:
                    reasons.append("tail_not_flat")
                    if status == "good":
                        status = "questionable"
                low, high = EQUILIBRIUM_SLOPE_RANGE
                slope = estimate.equilibrium_log_slope
                if band_method == "adaptive" and (
                    not math.isfinite(slope) or not low < slope < high
                ):
                    # Mudd et al. Section 2.1.3 discard these outright; the
                    # operational record keeps them, marked, for evaluation.
                    # The threshold is calibrated against an adaptively selected
                    # band, so it is not applied to the wider fixed band, whose
                    # slope is biased low by design.
                    reasons.append("equilibrium_slope_out_of_range")
                    if status == "good":
                        status = "questionable"
                if direction_name:
                    try:
                        direction = estimate_wind_direction(
                            subset[direction_name].values[local_index, :],
                            estimate.band_mask,
                            subset[spread_name].values[local_index, :] if spread_name else None,
                        )
                    except ValueError as error:
                        reasons.append(f"direction_unavailable: {error}")
                if status != "rejected":
                    u10, cd = inverse_coare_u10(estimate.friction_velocity, hs, tp)
            except Exception as error:
                reasons.append(str(error))
                status = "rejected"

            # Mudd et al. Section 5 bound the dependable range at 3-12 m/s. This
            # is reported alongside the estimate rather than folded into
            # qc_status, which describes the spectrum rather than the wind regime.
            if u10 is None:
                wind_reliability = None
            elif u10 < RELIABLE_U10_RANGE[0]:
                wind_reliability = "below_range"
            elif u10 > RELIABLE_U10_RANGE[1]:
                wind_reliability = "above_range"
            else:
                wind_reliability = "reliable"

            # Table 2: direction RMSE degrades from 13.2 to 56.2 deg below 7 m/s.
            if direction is None or u10 is None:
                direction_confidence = None
            elif u10 >= DIRECTION_CONFIDENCE_U10:
                direction_confidence = "high"
            else:
                direction_confidence = "low"

            records.append(
                {
                    "station_id": station_id,
                    "station_name": station["station_name"],
                    "time_utc": timestamp,
                    "estimated_u10_m_s": u10,
                    "estimated_wind_direction_deg_from": (
                        direction.direction_from_deg if direction else None
                    ),
                    "friction_velocity_m_s": estimate.friction_velocity if estimate else None,
                    "neutral_drag_coefficient": cd,
                    "direction_resultant_length": direction.resultant_length if direction else None,
                    "direction_circular_spread_deg": (
                        direction.circular_spread_deg if direction else None
                    ),
                    "mean_band_directional_spread_deg": (
                        direction.mean_band_spread_deg if direction else None
                    ),
                    "direction_bin_count": direction.bin_count if direction else 0,
                    "direction_confidence": direction_confidence,
                    "wind_reliability": wind_reliability,
                    "equilibrium_log_slope": estimate.equilibrium_log_slope if estimate else None,
                    "band_method": band_method,
                    "significant_wave_height_m": hs if math.isfinite(hs) else None,
                    "peak_period_s": tp if math.isfinite(tp) else None,
                    "peak_frequency_hz": estimate.peak_frequency if estimate else None,
                    "tail_min_hz": estimate.tail_min_frequency if estimate else None,
                    "tail_max_hz": estimate.tail_max_frequency if estimate else None,
                    "tail_bin_count": estimate.tail_bin_count if estimate else 0,
                    "mean_spectrum_f4_m2_hz3": estimate.mean_spectrum_f4 if estimate else None,
                    "tail_coefficient_of_variation": estimate.tail_cv if estimate else None,
                    "source_wave_qc_flag": source_qc_int,
                    "qc_status": status,
                    "qc_reasons": ";".join(reasons),
                    "source_url": source_url,
                    "estimator_version": VERSION,
                    "processed_at_utc": processed_at,
                }
            )

    return records, {"source_url": source_url, "available": int(indices.size), "processed": len(records)}


def atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", newline="", delete=False) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def latest_products(rows: list[dict[str, Any]], now: datetime, stale_hours: float) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("estimated_u10_m_s") in {None, ""}:
            continue
        station_id = str(row["station_id"])
        current = latest.get(station_id)
        if current is None or str(row["time_utc"]) > str(current["time_utc"]):
            normalized = dict(row)
            for field in JSON_FLOAT_FIELDS:
                value = normalized.get(field)
                normalized[field] = None if value in {None, ""} else float(value)
            for field in JSON_INT_FIELDS:
                value = normalized.get(field)
                normalized[field] = None if value in {None, ""} else int(float(value))
            latest[station_id] = normalized
    for row in latest.values():
        age_hours = (now - parse_utc(str(row["time_utc"]))).total_seconds() / 3600.0
        row["observation_age_hours"] = round(age_hours, 3)
        row["is_stale"] = age_hours > stale_hours
        bearing = row.get("estimated_wind_direction_deg_from")
        row["wind_direction_compass"] = compass_point(bearing) if bearing is not None else None
    return {"generated_at_utc": iso_utc(now), "estimator_version": VERSION, "stations": latest}


COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass_point(bearing_deg: float) -> str:
    """Sixteen-point compass label for a direction the wind blows from."""
    return COMPASS_POINTS[int((float(bearing_deg) % 360.0) / 22.5 + 0.5) % 16]


def clean_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_for_json(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/operational"))
    parser.add_argument("--station", action="append", choices=sorted(STATIONS), dest="stations")
    parser.add_argument("--initial-lookback-hours", type=float, default=48.0)
    parser.add_argument("--overlap-minutes", type=float, default=1.0)
    parser.add_argument("--stale-hours", type=float, default=2.0)
    parser.add_argument("--base-url", default=CDIP_REALTIME)
    parser.add_argument("--archive-url", default=CDIP_ARCHIVE)
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Rebuild from the real-time datasets only, skipping the CDIP archive.",
    )
    parser.add_argument(
        "--band-method",
        choices=["fixed", "adaptive"],
        default="fixed",
        help=(
            "Equilibrium band selection. 'fixed' uses 2 fp to %.2f Hz. 'adaptive' uses the "
            "Mudd et al. (2024) fitting method, which changes published U10 values and so "
            "warrants --rebuild-history." % TAIL_MAX_HZ
        ),
    )
    parser.add_argument(
        "--history-start",
        default=DEFAULT_HISTORY_START,
        help="UTC start time for the separately maintained historical CSV.",
    )
    parser.add_argument(
        "--rebuild-history",
        action="store_true",
        help="Reprocess both real-time datasets from --history-start.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.output_dir / ".operational_wind.lock"
    lock_stream = lock_path.open("w")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another operational wind run is active; exiting.")
        return 0

    now = utc_now()
    processed_at = iso_utc(now)
    csv_path = args.output_dir / "wind_estimates.csv"
    existing = read_existing(csv_path)
    known_latest = last_times(existing)
    history_start = parse_utc(args.history_start)
    chosen = args.stations or list(STATIONS)
    new_rows: list[dict[str, Any]] = []
    station_status: dict[str, Any] = {}
    failures = 0

    for key in chosen:
        station = STATIONS[key]
        latest = known_latest.get(station["station_id"])
        if args.rebuild_history:
            cutoff = history_start - timedelta(microseconds=1)
        else:
            cutoff = (
                latest - timedelta(minutes=args.overlap_minutes)
                if latest
                else now - timedelta(hours=args.initial_lookback_hours)
            )
        sources = []
        if args.rebuild_history and not args.no_archive:
            # The realtime window is only weeks to months long, so reaching back
            # to --history-start needs the archive dataset as well. Stations whose
            # archive predates the cutoff simply return no records.
            sources.append(archive_url(args.archive_url, station["station_id"]))
        sources.append(realtime_url(args.base_url, station["station_id"]))

        available = processed = 0
        errors: list[str] = []
        for source_url in sources:
            try:
                rows, status = process_station(
                    station, cutoff, processed_at, source_url, args.band_method
                )
                new_rows.extend(rows)
                available += status["available"]
                processed += status["processed"]
                print(
                    f"{station['station_name']}: received {status['available']}, "
                    f"processed {status['processed']} from {source_url.rsplit('/', 1)[-1]}"
                )
            except Exception as error:
                errors.append(f"{source_url}: {type(error).__name__}: {error}")
                print(
                    f"{station['station_name']}: ERROR reading {source_url}: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                )
        if errors:
            failures += 1
        station_status[station["station_id"]] = {
            "station_name": station["station_name"],
            "source_url": sources[-1],
            "sources": sources,
            "available": available,
            "processed": processed,
            "error": "; ".join(errors) or None,
        }

    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in [*existing, *new_rows]:
        keyed[(str(row["station_id"]), str(row["time_utc"]))] = row
    combined = sorted(keyed.values(), key=lambda row: (str(row["time_utc"]), str(row["station_id"])))
    atomic_write_csv(csv_path, combined)

    history_path = args.output_dir / "wind_estimates_since_2026.csv"
    history_keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in [*read_existing(history_path), *combined]:
        try:
            timestamp = parse_utc(str(row["time_utc"]))
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp >= history_start:
            history_keyed[(str(row["station_id"]), str(row["time_utc"]))] = row
    history_rows = sorted(
        history_keyed.values(),
        key=lambda row: (str(row["time_utc"]), str(row["station_id"])),
    )
    atomic_write_csv(history_path, history_rows)

    latest = latest_products(combined, now, args.stale_hours)
    atomic_write_json(args.output_dir / "latest.json", clean_for_json(latest))
    status = {
        "run_at_utc": processed_at,
        "success": failures == 0,
        "new_or_refreshed_records": len(new_rows),
        "total_records": len(combined),
        "band_method": args.band_method,
        "history_start_utc": iso_utc(history_start),
        "historical_records": len(history_rows),
        "historical_file": str(history_path.resolve()),
        "stations": station_status,
    }
    atomic_write_json(args.output_dir / "status.json", clean_for_json(status))
    print(f"Products updated in {args.output_dir.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
