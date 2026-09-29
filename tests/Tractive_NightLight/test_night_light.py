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
        "refresh_seconds": 120,
        "min_battery": 20,
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


class TestShouldLight(unittest.TestCase):
    def test_day_never_lights(self) -> None:
        self.assertFalse(night_light.should_light(False, False, 100, 20))

    def test_night_away_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, 80, 20))

    def test_night_at_home_stays_off(self) -> None:
        self.assertFalse(night_light.should_light(True, True, 80, 20))

    def test_low_battery_stays_off(self) -> None:
        self.assertFalse(night_light.should_light(True, False, 15, 20))

    def test_battery_at_floor_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, 20, 20))

    def test_unknown_battery_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, False, None, 20))
