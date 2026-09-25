import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_wind_plot import load_rows, render, station_notices  # noqa: E402

FIELDS = [
    "station_id", "station_name", "time_utc", "estimated_u10_m_s",
    "estimated_wind_direction_deg_from", "significant_wave_height_m", "peak_period_s",
    "qc_status", "qc_reasons", "direction_confidence",
]


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

    def test_notice_names_failed_station_only(self):
        status = self.dir / "status.json"
        status.write_text(json.dumps({"run_at_utc": "2026-09-25T18:23:12Z", "stations": {
            "249p1": {"station_name": "Arecibo", "error": None},
            "181p1": {"station_name": "Rincon", "error": "file not found"},
        }}))
        notices = station_notices(status, "249p1")
        self.assertIn("Rincon (181p1)", notices)
        self.assertNotIn("Arecibo", notices)

    def test_render_fills_placeholders_and_escapes_script_close(self):
        rows = [{"t": "2026-09-25T12:00:00Z", "why": "</script><b>"}]
        page = render(rows, "249p1", "Arecibo", "data/operational/wind_estimates.csv", "")
        self.assertNotIn("__", page.split("<script>")[0])
        self.assertNotIn("__DATA__", page)
        self.assertNotIn("</script><b>", page)


if __name__ == "__main__":
    unittest.main()
