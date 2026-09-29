"""Keep a Tractive tracker's LED lit for the duration of the night.

Tractive switches the LED off after a few minutes to spare the tracker's
battery. This module re-sends the "LED on" command at a shorter interval so
the light stays on until dawn, and stops doing so once the battery drops
below a configurable floor.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from aiotractive import Tractive
from astral import LocationInfo
from astral.sun import sun

LOGGER = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 120
DEFAULT_MIN_BATTERY = 20
RECONNECT_DELAY_SECONDS = 60
EARTH_RADIUS_METERS = 6_371_000
LATLONG_LENGTH = 2

# Values of ``sensor_used`` that mean the tracker located itself from a known
# Wi-Fi network rather than from GPS, i.e. it is at home.
WIFI_SENSORS = frozenset({"KNOWN_WIFI", "WIFI"})


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, normally read from the environment."""

    email: str
    password: str
    tracker_id: str | None
    location: LocationInfo
    refresh_seconds: int
    min_battery: int
    skip_when_home: bool
    home_latitude: float | None
    home_longitude: float | None
    home_radius_meters: float

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables.

        Raises:
            KeyError: if ``TRACTIVE_EMAIL`` or ``TRACTIVE_PASSWORD`` is missing.
        """
        home_latitude = os.environ.get("HOME_LATITUDE")
        home_longitude = os.environ.get("HOME_LONGITUDE")
        return cls(
            email=os.environ["TRACTIVE_EMAIL"],
            password=os.environ["TRACTIVE_PASSWORD"],
            tracker_id=os.environ.get("TRACTIVE_TRACKER_ID") or None,
            location=LocationInfo(
                name=os.environ.get("LOCATION_NAME", "Veyrier"),
                region=os.environ.get("LOCATION_REGION", "Switzerland"),
                timezone=os.environ.get("LOCATION_TIMEZONE", "Europe/Zurich"),
                latitude=float(os.environ.get("LOCATION_LATITUDE", "46.167")),
                longitude=float(os.environ.get("LOCATION_LONGITUDE", "6.183")),
            ),
            refresh_seconds=int(
                os.environ.get("REFRESH_SECONDS", str(DEFAULT_REFRESH_SECONDS))
            ),
            min_battery=int(os.environ.get("MIN_BATTERY", str(DEFAULT_MIN_BATTERY))),
            skip_when_home=os.environ.get("SKIP_WHEN_HOME", "true").lower()
            in {"1", "true", "yes"},
            home_latitude=float(home_latitude) if home_latitude else None,
            home_longitude=float(home_longitude) if home_longitude else None,
            home_radius_meters=float(os.environ.get("HOME_RADIUS_METERS", "0")),
        )


def is_night(location: LocationInfo, now: dt.datetime) -> bool:
    """Return whether ``now`` falls between civil dusk and civil dawn.

    Args:
        location: the place the sun times are computed for.
        now: a timezone-aware moment in time.

    Returns:
        True between dusk and dawn, False during the day.
    """
    events = sun(location.observer, date=now.date(), tzinfo=location.tzinfo)
    return now < events["dawn"] or now >= events["dusk"]


def distance_meters(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Return the great-circle distance between two points, in meters."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(haversine))


def is_at_home(pos_report: dict[str, Any], settings: Settings) -> bool:
    """Return whether the tracker is inside the power saving zone at home.

    ``sensor_used`` is the primary signal: Tractive stops using GPS and reports
    a known Wi-Fi network while the tracker sits at home. It is trusted over
    ``power_saving_zone_id``, which also appears in the hardware report and may
    therefore name the zone the tracker is assigned to rather than the one it
    currently sits in. When ``sensor_used`` is missing, the zone id is used
    instead, and an optional geofence around the home coordinates comes last.

    Args:
        pos_report: the raw position report returned by the Tractive API.
        settings: the runtime configuration.

    Returns:
        True if the tracker should be considered at home.
    """
    sensor_used = str(pos_report.get("sensor_used") or "").upper()
    if sensor_used:
        if sensor_used in WIFI_SENSORS:
            return True
    elif pos_report.get("power_saving_zone_id"):
        return True

    if (
        settings.home_radius_meters > 0
        and settings.home_latitude is not None
        and settings.home_longitude is not None
    ):
        latlong = pos_report.get("latlong") or []
        if len(latlong) == LATLONG_LENGTH:
            distance = distance_meters(
                float(latlong[0]),
                float(latlong[1]),
                settings.home_latitude,
                settings.home_longitude,
            )
            if distance <= settings.home_radius_meters:
                LOGGER.debug("Tracker is %.0f m from home.", distance)
                return True

    return False


def should_light(
    night: bool, at_home: bool, battery: int | None, min_battery: int
) -> bool:
    """Decide whether the LED should be on right now.

    Args:
        night: whether it is currently night.
        at_home: whether the tracker sits in the power saving zone.
        battery: the tracker's battery level in percent, or None if unknown.
        min_battery: the level below which the LED is left off.

    Returns:
        True if the LED should be lit.
    """
    if not night:
        return False
    if at_home:
        return False
    if battery is not None and battery < min_battery:
        LOGGER.warning("Battery at %s%%, leaving the LED off.", battery)
        return False
    return True


async def _pick_tracker(client: Tractive, tracker_id: str | None) -> Any:
    """Return the configured tracker, or the first one on the account."""
    if tracker_id:
        return client.tracker(tracker_id)
    trackers = await client.trackers()
    if not trackers:
        raise RuntimeError("No tracker found on this Tractive account.")
    return trackers[0]


async def inspect(settings: Settings) -> None:
    """Print the raw tracker payloads, to check field names against the API."""
    async with Tractive(settings.email, settings.password) as client:
        tracker = await _pick_tracker(client, settings.tracker_id)
        print("--- hw_info ---")
        print(json.dumps(await tracker.hw_info(), indent=2, sort_keys=True))
        print("--- pos_report ---")
        pos_report = await tracker.pos_report()
        print(json.dumps(pos_report, indent=2, sort_keys=True))
        print(f"--- is_at_home: {is_at_home(pos_report, settings)} ---")


async def _session(settings: Settings, force_night: bool) -> None:
    """Run one connected session, looping until it fails."""
    async with Tractive(settings.email, settings.password) as client:
        tracker = await _pick_tracker(client, settings.tracker_id)
        led_on: bool | None = None

        while True:
            night = force_night or is_night(
                settings.location, dt.datetime.now(settings.location.tzinfo)
            )

            at_home = False
            battery: int | None = None
            if night:
                if settings.skip_when_home:
                    at_home = is_at_home(await tracker.pos_report(), settings)
                if not at_home:
                    hw_info = await tracker.hw_info()
                    battery = hw_info.get("battery_level")

            wanted = should_light(night, at_home, battery, settings.min_battery)

            # The "on" command is re-sent every cycle: Tractive expires it.
            if wanted or led_on is not False:
                await tracker.set_led_active(wanted)
            if wanted != led_on:
                LOGGER.info(
                    "LED %s%s.",
                    "on" if wanted else "off",
                    " (tracker at home)" if at_home else "",
                )
            led_on = wanted

            await asyncio.sleep(settings.refresh_seconds)


async def run(settings: Settings, force_night: bool = False) -> None:
    """Run the night light forever, reconnecting after any failure.

    Args:
        settings: the runtime configuration.
        force_night: treat every moment as night, for testing in daylight.
    """
    while True:
        try:
            await _session(settings, force_night)
        except asyncio.CancelledError:
            raise
        except Exception:  # the service must survive anything
            LOGGER.exception(
                "Session failed, retrying in %ss.", RECONNECT_DELAY_SECONDS
            )
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
