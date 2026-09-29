import datetime as dt
import unittest
import zoneinfo

from astral import LocationInfo

from Tractive_NightLight import night_light

VEYRIER = LocationInfo("Veyrier", "Switzerland", "Europe/Zurich", 46.167, 6.183)
TZ = zoneinfo.ZoneInfo("Europe/Zurich")


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


class TestShouldLight(unittest.TestCase):
    def test_day_never_lights(self) -> None:
        self.assertFalse(night_light.should_light(False, 100, 20))

    def test_night_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, 80, 20))

    def test_low_battery_stays_off(self) -> None:
        self.assertFalse(night_light.should_light(True, 15, 20))

    def test_battery_at_floor_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, 20, 20))

    def test_unknown_battery_lights(self) -> None:
        self.assertTrue(night_light.should_light(True, None, 20))
