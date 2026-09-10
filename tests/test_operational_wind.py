import math
import unittest
from datetime import datetime, timezone

import numpy as np

from operational_wind import (
    BETA,
    GRAVITY,
    IP,
    compass_point,
    equilibrium_log_slope,
    estimate_friction_velocity,
    estimate_wind_direction,
    inverse_coare_u10,
    latest_products,
)


class FrictionVelocityTests(unittest.TestCase):
    def test_flat_f4_tail_recovers_expected_level(self):
        frequency = np.arange(0.05, 0.61, 0.01)
        target_level = 0.0012
        energy = target_level / frequency**4
        result = estimate_friction_velocity(frequency, energy, peak_period_s=10.0)

        expected_ustar = target_level * (2 * np.pi) ** 3 / (4 * BETA * IP * GRAVITY)
        self.assertAlmostEqual(result.mean_spectrum_f4, target_level, places=12)
        self.assertAlmostEqual(result.friction_velocity, expected_ustar, places=12)
        self.assertAlmostEqual(result.tail_cv, 0.0, places=12)
        expected_bins = np.count_nonzero((frequency >= 0.2) & (frequency <= 0.5))
        self.assertEqual(result.tail_bin_count, expected_bins)

    def test_requires_enough_equilibrium_bins(self):
        frequency = np.array([0.1, 0.2, 0.3, 0.6])
        energy = np.ones_like(frequency)
        with self.assertRaisesRegex(ValueError, "at least 3"):
            estimate_friction_velocity(frequency, energy, peak_period_s=5.0)

    def test_rejects_invalid_peak_period(self):
        with self.assertRaisesRegex(ValueError, "peak period"):
            estimate_friction_velocity(np.array([0.2, 0.3, 0.4]), np.ones(3), math.nan)

    def test_inverse_coare_matches_validated_experimental_case(self):
        u10, cd = inverse_coare_u10(0.1738003114322509, 1.04, 7.69)
        self.assertAlmostEqual(u10, 5.511647964355956, places=10)
        self.assertAlmostEqual(cd, 0.0009766963047116851, places=12)

    def test_latest_product_restores_numeric_csv_values(self):
        row = {
            "station_id": "249p1",
            "time_utc": "2026-08-06T20:00:00Z",
            "estimated_u10_m_s": "9.15",
            "tail_bin_count": "14",
            "source_wave_qc_flag": "1",
        }
        product = latest_products(
            [row],
            datetime(2026, 8, 6, 21, tzinfo=timezone.utc),
            stale_hours=2,
        )
        latest = product["stations"]["249p1"]
        self.assertEqual(latest["estimated_u10_m_s"], 9.15)
        self.assertEqual(latest["tail_bin_count"], 14)
        self.assertFalse(latest["is_stale"])

    def test_parse_utc_accepts_operational_timestamp(self):
        from operational_wind import parse_utc

        parsed = parse_utc("2026-01-01T00:00:00Z")
        self.assertEqual(parsed, datetime(2026, 1, 1, tzinfo=timezone.utc))


class EquilibriumBandTests(unittest.TestCase):
    def setUp(self):
        self.frequency = np.arange(0.05, 0.581, 0.01)
        self.energy = 0.0012 / self.frequency**4

    def test_log_slope_of_phillips_spectrum_is_minus_four(self):
        mask = self.frequency >= 0.2
        slope = equilibrium_log_slope(self.frequency, self.energy, mask)
        self.assertAlmostEqual(slope, -4.0, places=9)

    def test_adaptive_band_matches_fixed_level_on_flat_tail(self):
        """A perfectly flat E(f)f^4 tail must give both methods the same u*."""
        fixed = estimate_friction_velocity(self.frequency, self.energy, 10.0)
        adaptive = estimate_friction_velocity(
            self.frequency, self.energy, 10.0, band_method="adaptive"
        )
        self.assertEqual(adaptive.band_method, "adaptive")
        self.assertAlmostEqual(adaptive.friction_velocity, fixed.friction_velocity, places=12)

    def test_adaptive_band_avoids_high_frequency_rolloff(self):
        """Biofouling damps the tail; the fit should retreat below the rolloff."""
        energy = self.energy.copy()
        rolloff = self.frequency > 0.35
        energy[rolloff] *= np.exp(-40.0 * (self.frequency[rolloff] - 0.35))
        adaptive = estimate_friction_velocity(
            self.frequency, energy, 10.0, band_method="adaptive"
        )
        self.assertLessEqual(adaptive.tail_max_frequency, 0.36)
        self.assertGreater(adaptive.equilibrium_log_slope, -4.4)

        damped = estimate_friction_velocity(self.frequency, energy, 10.0)
        self.assertLess(damped.equilibrium_log_slope, -4.4)
        self.assertLess(damped.friction_velocity, adaptive.friction_velocity)

    def test_usable_bins_exclude_flagged_frequencies(self):
        usable = self.frequency <= 0.40
        estimate = estimate_friction_velocity(
            self.frequency, self.energy, 10.0, usable_bins=usable
        )
        self.assertLessEqual(estimate.tail_max_frequency, 0.40)

    def test_rejects_unknown_band_method(self):
        with self.assertRaisesRegex(ValueError, "unknown band method"):
            estimate_friction_velocity(self.frequency, self.energy, 10.0, band_method="best")


class WindDirectionTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.array([False, True, True, True, True, False])

    def test_circular_mean_of_constant_direction(self):
        directions = np.array([10.0, 45.0, 45.0, 45.0, 45.0, 300.0])
        result = estimate_wind_direction(directions, self.mask)
        self.assertAlmostEqual(result.direction_from_deg, 45.0, places=9)
        self.assertAlmostEqual(result.resultant_length, 1.0, places=12)
        self.assertAlmostEqual(result.circular_spread_deg, 0.0, places=9)
        self.assertEqual(result.bin_count, 4)

    def test_circular_mean_wraps_across_north(self):
        """The arithmetic mean of these is 180 deg; the circular mean is 0."""
        directions = np.array([90.0, 350.0, 10.0, 355.0, 5.0, 90.0])
        result = estimate_wind_direction(directions, self.mask)
        self.assertAlmostEqual(result.direction_from_deg, 0.0, places=9)

    def test_only_masked_bands_contribute(self):
        directions = np.array([200.0, 30.0, 30.0, 30.0, 30.0, 200.0])
        self.assertAlmostEqual(
            estimate_wind_direction(directions, self.mask).direction_from_deg, 30.0, places=9
        )

    def test_opposed_directions_have_no_mean(self):
        directions = np.array([0.0, 0.0, 180.0, 0.0, 180.0, 0.0])
        with self.assertRaisesRegex(ValueError, "cancel"):
            estimate_wind_direction(directions, self.mask)

    def test_scattered_directions_raise_circular_spread(self):
        tight = estimate_wind_direction(np.array([0, 40, 45, 50, 45, 0.0]), self.mask)
        loose = estimate_wind_direction(np.array([0, 10, 80, 350, 100, 0.0]), self.mask)
        self.assertLess(tight.circular_spread_deg, loose.circular_spread_deg)
        self.assertGreater(tight.resultant_length, loose.resultant_length)

    def test_requires_minimum_directional_bins(self):
        directions = np.array([1.0, np.nan, np.nan, 45.0, np.nan, 1.0])
        with self.assertRaisesRegex(ValueError, "at least 3"):
            estimate_wind_direction(directions, self.mask)

    def test_mean_band_spread_averages_over_band_only(self):
        directions = np.full(6, 45.0)
        spreads = np.array([90.0, 10.0, 20.0, 30.0, 40.0, 90.0])
        result = estimate_wind_direction(directions, self.mask, spreads)
        self.assertAlmostEqual(result.mean_band_spread_deg, 25.0, places=9)

    def test_direction_uses_the_same_band_as_the_speed(self):
        """Mudd et al. average direction over the band that produced the speed."""
        frequency = np.arange(0.05, 0.581, 0.01)
        energy = 0.0012 / frequency**4
        estimate = estimate_friction_velocity(frequency, energy, 10.0)
        directions = np.where(frequency >= estimate.tail_min_frequency, 120.0, 300.0)
        result = estimate_wind_direction(directions, estimate.band_mask)
        self.assertAlmostEqual(result.direction_from_deg, 120.0, places=9)
        self.assertEqual(result.bin_count, estimate.tail_bin_count)


class CompassTests(unittest.TestCase):
    def test_known_bearings(self):
        for bearing, label in [
            (0, "N"), (22.5, "NNE"), (90, "E"), (180, "S"),
            (270, "W"), (340, "NNW"), (359, "N"), (360, "N"),
            (11.24, "N"), (11.26, "NNE"),
        ]:
            self.assertEqual(compass_point(bearing), label, msg=f"{bearing} deg")


if __name__ == "__main__":
    unittest.main()
