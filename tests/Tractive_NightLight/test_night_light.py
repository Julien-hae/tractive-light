import datetime as dt
import unittest
import zoneinfo
from typing import Any

from astral import LocationInfo

from Tractive_NightLight import night_light

VEYRIER = LocationInfo("Veyrier", "Switzerland", "Europe/Zurich", 46.167, 6.183)
TZ = zoneinfo.ZoneInfo("Europe/Zurich")


def make_settings(**overrides: Any) -> night_light.Settings:
    defaults: dict[str, Any] = {
        "email": "user@example.com",
        "password": "secret",
        "tracker_id": None,
        "location": VEYRIER,
        "refresh_seconds": 240,
        "idle_poll_seconds": 900,
        "min_battery": 30,
        "max_led_minutes": 120,
        "light_windows": night_light.parse_windows(None),
        "skip_when_home": True,
        "home_latitude": None,
        "home_longitude": None,
        "home_radius_meters": 0.0,
    }
    defaults.update(overrides)
    return night_light.Settings(**defaults)


class TestIsNight(unittest.TestCase):
    def test_midday_in_june_is_day(self) -> None:
        noon = dt.datetime(2026, 6, 21, 12, 0, tzinfo=TZ)
        self.assertFalse(night_light.is_night(VEYRIER, noon))

    def test_midnight_in_june_is_night(self) -> None:
        midnight = dt.datetime(2026, 6, 21, 0, 30, tzinfo=TZ)
        self.assertTrue(night_light.is_night(VEYRIER, midnight))

    def test_winter_evening_is_night(self) -> None:
        evening = dt.datetime(2026, 12, 21, 18, 0, tzinfo=TZ)
        self.assertTrue(night_light.is_night(VEYRIER, evening))


class TestIsAtHome(unittest.TestCase):
    def test_known_wifi_sensor_means_home(self) -> None:
        report = {
            "sensor_used": "KNOWN_WIFI",
            "power_saving_zone_id": "6a16dfac5802f3993da028c7",
        }
        self.assertTrue(night_light.is_at_home(report, make_settings()))

    def test_gps_sensor_wins_over_zone_id(self) -> None:
        # The zone id also shows up in hw_info, so it may name the tracker's
        # assigned zone rather than its current one: sensor_used decides.
        report = {
            "sensor_used": "GPS",
            "power_saving_zone_id": "6a16dfac5802f3993da028c7",
        }
        self.assertFalse(night_light.is_at_home(report, make_settings()))

    def test_zone_id_used_when_sensor_missing(self) -> None:
        report = {"power_saving_zone_id": "6a16dfac5802f3993da028c7"}
        self.assertTrue(night_light.is_at_home(report, make_settings()))

    def test_empty_report_is_not_home(self) -> None:
        self.assertFalse(night_light.is_at_home({}, make_settings()))

    def test_gps_far_away_is_not_home(self) -> None:
        report = {"sensor_used": "GPS", "latlong": [46.2, 6.25]}
        settings = make_settings(
            home_latitude=46.178263, home_longitude=6.163452, home_radius_meters=100
        )
        self.assertFalse(night_light.is_at_home(report, settings))

    def test_gps_inside_radius_is_home(self) -> None:
        report = {"sensor_used": "GPS", "latlong": [46.178300, 6.163500]}
        settings = make_settings(
            home_latitude=46.178263, home_longitude=6.163452, home_radius_meters=100
        )
        self.assertTrue(night_light.is_at_home(report, settings))

    def test_radius_disabled_ignores_coordinates(self) -> None:
        report = {"sensor_used": "GPS", "latlong": [46.178263, 6.163452]}
        settings = make_settings(home_latitude=46.178263, home_longitude=6.163452)
        self.assertFalse(night_light.is_at_home(report, settings))


class TestDistanceMeters(unittest.TestCase):
    def test_same_point_is_zero(self) -> None:
        self.assertEqual(night_light.distance_meters(46.0, 6.0, 46.0, 6.0), 0.0)

    def test_known_distance(self) -> None:
        # One degree of latitude is roughly 111 km.
        distance = night_light.distance_meters(46.0, 6.0, 47.0, 6.0)
        self.assertAlmostEqual(distance, 111_195, delta=500)


class TestParseWindows(unittest.TestCase):
    def test_empty_defaults_to_dusk_dawn(self) -> None:
        windows = night_light.parse_windows(None)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].start, night_light.DUSK)
        self.assertEqual(windows[0].end, night_light.DAWN)

    def test_two_windows(self) -> None:
        windows = night_light.parse_windows("dusk-22:00,04:00-dawn")
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].start, night_light.DUSK)
        self.assertEqual(windows[0].end, dt.time(22, 0))
        self.assertEqual(windows[1].start, dt.time(4, 0))
        self.assertEqual(windows[1].end, night_light.DAWN)

    def test_labels_are_kept_for_logging(self) -> None:
        windows = night_light.parse_windows(" dusk-22:00 , 04:00-dawn ")
        self.assertEqual([w.label for w in windows], ["dusk-22:00", "04:00-dawn"])

    def test_case_is_ignored(self) -> None:
        windows = night_light.parse_windows("DUSK-DAWN")
        self.assertEqual(windows[0].start, night_light.DUSK)

    def test_missing_separator_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            night_light.parse_windows("22:00")

    def test_missing_end_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            night_light.parse_windows("22:00-")

    def test_unknown_bound_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            night_light.parse_windows("dusk-minuit")


class TestNightBounds(unittest.TestCase):
    def test_evening_belongs_to_the_night_starting_today(self) -> None:
        now = dt.datetime(2026, 10, 7, 21, 0, tzinfo=TZ)
        start, end = night_light.night_bounds(VEYRIER, now)
        self.assertEqual(start.date(), dt.date(2026, 10, 7))
        self.assertEqual(end.date(), dt.date(2026, 10, 8))

    def test_early_morning_belongs_to_yesterdays_night(self) -> None:
        now = dt.datetime(2026, 10, 7, 5, 0, tzinfo=TZ)
        start, end = night_light.night_bounds(VEYRIER, now)
        self.assertEqual(start.date(), dt.date(2026, 10, 6))
        self.assertEqual(end.date(), dt.date(2026, 10, 7))

    def test_now_is_inside_its_own_night(self) -> None:
        for hour in (3, 5, 20, 23):
            now = dt.datetime(2026, 10, 7, hour, tzinfo=TZ)
            start, end = night_light.night_bounds(VEYRIER, now)
            self.assertLessEqual(start, now, f"hour {hour}")
            self.assertLess(now, end, f"hour {hour}")


class TestActiveWindow(unittest.TestCase):
    WINDOWS = night_light.parse_windows("dusk-22:00,04:00-dawn")

    def active(self, hour: int, minute: int = 0) -> str | None:
        now = dt.datetime(2026, 10, 7, hour, minute, tzinfo=TZ)
        window = night_light.active_window(self.WINDOWS, VEYRIER, now)
        return window.label if window else None

    def test_just_after_dusk_opens_the_evening_window(self) -> None:
        # Dusk is 19:34 on 7 October 2026 in Veyrier.
        self.assertEqual(self.active(19, 40), "dusk-22:00")

    def test_just_before_dusk_is_closed(self) -> None:
        self.assertIsNone(self.active(19, 20))

    def test_quiet_middle_of_the_night_is_closed(self) -> None:
        self.assertIsNone(self.active(23, 30))
        self.assertIsNone(self.active(2, 0))

    def test_early_morning_opens_the_morning_window(self) -> None:
        self.assertEqual(self.active(5, 0), "04:00-dawn")

    def test_just_before_four_is_closed(self) -> None:
        self.assertIsNone(self.active(3, 55))

    def test_after_dawn_is_closed(self) -> None:
        # Dawn is 07:11 on 7 October 2026.
        self.assertIsNone(self.active(7, 30))

    def test_midday_is_closed(self) -> None:
        self.assertIsNone(self.active(12, 0))

    def test_default_window_covers_the_whole_night(self) -> None:
        windows = night_light.parse_windows(None)
        for hour in (20, 23, 2, 5):
            now = dt.datetime(2026, 10, 7, hour, tzinfo=TZ)
            self.assertIsNotNone(
                night_light.active_window(windows, VEYRIER, now), f"hour {hour}"
            )

    def test_window_crossing_midnight(self) -> None:
        windows = night_light.parse_windows("22:00-02:00")
        for hour, expected in (
            (21, None),
            (23, "22:00-02:00"),
            (1, "22:00-02:00"),
            (3, None),
        ):
            now = dt.datetime(2026, 10, 7, hour, tzinfo=TZ)
            window = night_light.active_window(windows, VEYRIER, now)
            self.assertEqual(window.label if window else None, expected, f"hour {hour}")


class TestBudgetExhausted(unittest.TestCase):
    def test_zero_budget_means_unlimited(self) -> None:
        self.assertFalse(night_light.budget_exhausted(99_999.0, 0))

    def test_fresh_night_has_allowance(self) -> None:
        self.assertFalse(night_light.budget_exhausted(0.0, 120))

    def test_just_under_budget(self) -> None:
        self.assertFalse(night_light.budget_exhausted(119 * 60, 120))

    def test_budget_reached(self) -> None:
        self.assertTrue(night_light.budget_exhausted(120 * 60, 120))


class TestShouldLight(unittest.TestCase):
    def test_not_allowed_never_lights(self) -> None:
        self.assertFalse(night_light.should_light(False, False, 100, 30))

    def test_allowed_away_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, 80, 30))

    def test_at_home_stays_off(self) -> None:
        self.assertFalse(night_light.should_light(True, True, 80, 30))

    def test_low_battery_stays_off(self) -> None:
        self.assertFalse(night_light.should_light(True, False, 25, 30))

    def test_battery_at_floor_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, 30, 30))

    def test_unknown_battery_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, None, 30))
