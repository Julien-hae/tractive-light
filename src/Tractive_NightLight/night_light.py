"""Keep a Tractive tracker's LED lit for the duration of the night.

Tractive switches the LED off after a few minutes to spare the tracker's
battery. This module re-sends the "LED on" command at a shorter interval so
the light stays on until dawn, and stops doing so once the battery drops
below a configurable floor.
"""

import asyncio
import datetime as dt
import logging
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


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, normally read from the environment."""

    email: str
    password: str
    tracker_id: str | None
    location: LocationInfo
    refresh_seconds: int
    min_battery: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables.

        Raises:
            KeyError: if ``TRACTIVE_EMAIL`` or ``TRACTIVE_PASSWORD`` is missing.
        """
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


def should_light(night: bool, battery: int | None, min_battery: int) -> bool:
    """Decide whether the LED should be on right now.

    Args:
        night: whether it is currently night.
        battery: the tracker's battery level in percent, or None if unknown.
        min_battery: the level below which the LED is left off.

    Returns:
        True if the LED should be lit.
    """
    if not night:
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


async def _session(settings: Settings, force_night: bool) -> None:
    """Run one connected session, looping until it fails."""
    async with Tractive(settings.email, settings.password) as client:
        tracker = await _pick_tracker(client, settings.tracker_id)
        led_on: bool | None = None

        while True:
            night = force_night or is_night(
                settings.location, dt.datetime.now(settings.location.tzinfo)
            )

            battery: int | None = None
            if night:
                hw_info = await tracker.hw_info()
                battery = hw_info.get("battery_level")

            wanted = should_light(night, battery, settings.min_battery)

            # The "on" command is re-sent every cycle: Tractive expires it.
            if wanted or led_on is not False:
                await tracker.set_led_active(wanted)
            if wanted != led_on:
                LOGGER.info("LED %s.", "on" if wanted else "off")
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
