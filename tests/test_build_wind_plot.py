import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_wind_plot import load_rows, render, station_notices, validate  # noqa: E402
from validation_wind import (  # noqa: E402
    Observations,
    angle_difference,
    comparison_stats,
    match_to_buoy,
    parse_ascii,
    parse_time_units,
    variable_attributes,
)

FIELDS = [
    "station_id", "station_name", "time_utc", "estimated_u10_m_s",
    "estimated_wind_direction_deg_from", "significant_wave_height_m", "peak_period_s",
    "qc_status", "qc_reasons", "direction_confidence",
]
UTC = timezone.utc


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def row(time_utc, station_id="249p1", u10="5.0", qc="good", reasons=""):
    return {
        "station_id": station_id, "station_name": "Arecibo" if station_id == "249p1" else "Rincon",
        "time_utc": time_utc, "estimated_u10_m_s": u10, "estimated_wind_direction_deg_from": "60",
        "significant_wave_height_m": "0.7", "peak_period_s": "10", "qc_status": qc,
        "qc_reasons": reasons, "direction_confidence": "low",
    }


def observations(samples):
    obs = Observations("X", "Test station", "10 m", "https://example.invalid/x.nc")
    for when, speed, direction in samples:
        obs.times.append(when)
        obs.speed_m_s.append(speed)
        obs.direction_deg.append(direction)
    return obs


class BuildWindPlotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.dir = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_filters_station_and_window_and_sorts(self):
        path = self.dir / "w.csv"
        write_csv(path, [
            row("2026-09-25T12:00:00Z"),
            row("2026-09-10T00:00:00Z"),
            row("2026-09-24T12:00:00Z", station_id="181p1"),
            row("2026-09-24T12:00:00Z", u10="", qc="rejected"),
        ])
        rows, name = load_rows(path, "249p1", days=7)
        self.assertEqual(name, "Arecibo")
        self.assertEqual([r["t"] for r in rows], ["2026-09-24T12:00:00Z", "2026-09-25T12:00:00Z"])
        self.assertIsNone(rows[0]["u"])

    def test_notice_flags_failed_buoy_and_unreadable_station(self):
        status = self.dir / "status.json"
        status.write_text(json.dumps({"run_at_utc": "2026-09-25T18:23:12Z", "stations": {
            "249p1": {"station_name": "Arecibo", "error": None},
            "181p1": {"station_name": "Rincon", "error": "file not found"},
        }}))
        self.assertEqual(station_notices(status, "249p1", None), "")
        self.assertIn("CDIP 181p1", station_notices(status, "181p1", None))
        broken = observations([])
        broken.error = "URLError: timed out"
        self.assertIn("Test station could not be read", station_notices(status, "249p1", broken))

    def test_render_fills_placeholders_and_escapes_script_close(self):
        rows = [{"t": "2026-09-25T12:00:00Z", "why": "</script><b>"}]
        page = render(rows, "249p1", "Arecibo", "data/operational/wind_estimates.csv", "")
        self.assertNotIn("__", page.split("<script>")[0])
        self.assertNotIn("__DATA__", page)
        self.assertNotIn("</script><b>", page)

    def test_validate_skips_rejected_rows(self):
        t = datetime(2026, 9, 25, 12, tzinfo=UTC)
        rows = [
            {"t": "2026-09-25T12:00:00Z", "u": 5.0, "d": 90.0, "qc": "good"},
            {"t": "2026-09-25T12:30:00Z", "u": 3.0, "d": 90.0, "qc": "questionable"},
            {"t": "2026-09-25T13:00:00Z", "u": None, "d": None, "qc": "rejected"},
        ]
        obs = observations([(t + timedelta(minutes=m), 4.0, 80.0) for m in range(0, 90, 10)])
        matched, stats = validate(rows, obs)
        self.assertEqual(matched[0], {"u": 4.0, "d": 80.0})
        self.assertEqual(stats["speed_all"]["n"], 2)
        self.assertAlmostEqual(stats["speed_all"]["bias"], 0.0)
        self.assertIsNone(stats["speed_good"])
        self.assertAlmostEqual(stats["direction_all"]["bias"], 10.0)


class ValidationWindTests(unittest.TestCase):
    def test_parse_ascii_handles_station_dimension(self):
        text = (
            "Dataset {\n    Float64 time[time = 2];\n} x.nc;\n"
            "---------------------------------------------\n"
            "time[2]\n20721.5, 20721.6\n\n"
            "wind_speed[1][2]\n[0], 1.5, -999.0\n\n"
            "AvrgWS[2]\n3.0, NaN\n"
        )
        arrays = parse_ascii(text)
        self.assertEqual(arrays["time"], [20721.5, 20721.6])
        self.assertEqual(arrays["wind_speed"], [1.5, -999.0])
        self.assertEqual(arrays["AvrgWS"][0], 3.0)

    def test_time_units_and_attributes(self):
        step, epoch = parse_time_units("days since 2018-12-03 14:17:00 +0:00")
        self.assertEqual(step, 86400.0)
        self.assertEqual(epoch, datetime(2018, 12, 3, 14, 17, tzinfo=UTC))
        das = 'Attributes {\n    AvrgWS {\n        String units "miles per hour";\n    }\n}\n'
        self.assertEqual(variable_attributes(das, "AvrgWS")["units"], "miles per hour")

    def test_match_averages_window_and_direction_across_north(self):
        t = datetime(2026, 9, 25, 12, tzinfo=UTC)
        obs = observations([
            (t - timedelta(minutes=5), 99.0, 180.0),
            (t, 2.0, 350.0),
            (t + timedelta(minutes=10), 4.0, 10.0),
            (t + timedelta(minutes=30), 99.0, 180.0),
        ])
        [(speed, direction)] = match_to_buoy([t], obs)
        self.assertAlmostEqual(speed, 3.0)
        self.assertAlmostEqual(angle_difference(direction, 0.0), 0.0, places=6)

    def test_comparison_stats(self):
        stats = comparison_stats([(3.0, 2.0), (5.0, 4.0), (7.0, 5.0)])
        self.assertAlmostEqual(stats["bias"], 4 / 3)
        self.assertGreater(stats["r"], 0.9)
        self.assertIsNone(comparison_stats([(1.0, 1.0)]))
        self.assertAlmostEqual(comparison_stats([(355.0, 5.0), (5.0, 355.0)], circular=True)["rmse"], 10.0)


if __name__ == "__main__":
    unittest.main()
