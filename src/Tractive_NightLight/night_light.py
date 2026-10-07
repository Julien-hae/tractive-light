"""Keep a Tractive tracker's LED lit during the night, within a battery budget.

Tractive switches the LED off six minutes after the command, to spare the
tracker's battery. This module re-sends the "LED on" command at a shorter
interval so the light stays on, and keeps the cost bounded three ways: one or
more lighting windows inside the night, a budget of lit minutes per window, and
a battery floor. When lighting is not possible the loop falls back to a long
idle interval, so the tracker is left alone rather than polled every two
minutes.

Windows are what make the cost worth paying: the LED earns its battery during
the evening and early-morning traffic, not in the quiet middle of the night. A
window bound is either a local ``HH:MM`` time or one of the sun events ``dusk``
and ``dawn``, so ``dusk-22:00,04:00-dawn`` follows the seasons on both ends.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Final, Literal

from aiotractive import Tractive
from astral import LocationInfo
from astral.sun import sun

LOGGER = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 240
DEFAULT_IDLE_POLL_SECONDS = 900
DEFAULT_MIN_BATTERY = 30
DEFAULT_MAX_LED_MINUTES = 120
RECONNECT_DELAY_SECONDS = 60
EARTH_RADIUS_METERS = 6_371_000
LATLONG_LENGTH = 2
SECONDS_PER_MINUTE = 60

type SunEvent = Literal["dusk", "dawn"]
type Bound = dt.time | SunEvent

DUSK: Final[SunEvent] = "dusk"
DAWN: Final[SunEvent] = "dawn"
DEFAULT_WINDOWS_RAW: Final = f"{DUSK}-{DAWN}"

# Values of ``sensor_used`` that mean the tracker located itself from a known
# Wi-Fi network rather than from GPS, i.e. it is at home.
WIFI_SENSORS = frozenset({"KNOWN_WIFI", "WIFI"})


@dataclass(frozen=True)
class LightWindow:
    """One stretch of the night during which the LED may be lit.

    Attributes:
        start: the opening bound, a local time or a sun event.
        end: the closing bound, a local time or a sun event.
        label: the window as written in the configuration, for logging.
    """

    start: Bound
    end: Bound
    label: str


def parse_bound(raw: str) -> Bound:
    """Parse one window bound: ``dusk``, ``dawn``, or a ``HH:MM`` local time.

    Args:
        raw: the bound as written in the configuration.

    Returns:
        The sun event name, or the parsed time.

    Raises:
        ValueError: if ``raw`` is neither a sun event nor a valid time.
    """
    value = raw.strip().lower()
    if value == DUSK:
        return DUSK
    if value == DAWN:
        return DAWN
    return dt.time.fromisoformat(value)


def parse_windows(raw: str | None) -> tuple[LightWindow, ...]:
    """Parse a comma-separated list of windows such as ``dusk-22:00,04:00-dawn``.

    Args:
        raw: the configured value; empty means one window from dusk to dawn.

    Returns:
        The parsed windows, in the order given.

    Raises:
        ValueError: if a window is not of the form ``START-END``, or a bound
            cannot be parsed.
    """
    if not raw or not raw.strip():
        raw = DEFAULT_WINDOWS_RAW

    windows: list[LightWindow] = []
    for chunk in raw.split(","):
        label = chunk.strip()
        if not label:
            continue
        start_raw, separator, end_raw = label.partition("-")
        if not separator or not end_raw.strip():
            msg = f"Window {label!r} is not of the form START-END."
            raise ValueError(msg)
        windows.append(LightWindow(parse_bound(start_raw), parse_bound(end_raw), label))

    if not windows:
        msg = "LIGHT_WINDOWS is set but lists no window."
        raise ValueError(msg)
    return tuple(windows)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, normally read from the environment."""

    email: str
    password: str
    tracker_id: str | None
    location: LocationInfo
    refresh_seconds: int
    idle_poll_seconds: int
    min_battery: int
    max_led_minutes: int
    light_windows: tuple[LightWindow, ...]
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
            idle_poll_seconds=int(
                os.environ.get("IDLE_POLL_SECONDS", str(DEFAULT_IDLE_POLL_SECONDS))
            ),
            min_battery=int(os.environ.get("MIN_BATTERY", str(DEFAULT_MIN_BATTERY))),
            max_led_minutes=int(
                os.environ.get("MAX_LED_MINUTES", str(DEFAULT_MAX_LED_MINUTES))
            ),
            light_windows=parse_windows(os.environ.get("LIGHT_WINDOWS")),
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


def night_bounds(
    location: LocationInfo, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    """Return the dusk and dawn bracketing the night that contains ``now``.

    A night spans two calendar dates, so an early-morning moment belongs to the
    night that began at the previous day's dusk.

    Args:
        location: the place the sun times are computed for.
        now: a timezone-aware moment in time.

    Returns:
        The night's starting dusk and closing dawn, as datetimes.
    """
    today = sun(location.observer, date=now.date(), tzinfo=location.tzinfo)
    if now < today["dawn"]:
        yesterday = sun(
            location.observer,
            date=now.date() - dt.timedelta(days=1),
            tzinfo=location.tzinfo,
        )
        return yesterday["dusk"], today["dawn"]
    tomorrow = sun(
        location.observer,
        date=now.date() + dt.timedelta(days=1),
        tzinfo=location.tzinfo,
    )
    return today["dusk"], tomorrow["dawn"]


def resolve_bound(
    bound: Bound,
    night_start: dt.datetime,
    night_end: dt.datetime,
    not_before: dt.datetime,
) -> dt.datetime:
    """Place a window bound on the calendar, inside the night at hand.

    A clock bound is placed on the first date at which it falls at or after
    ``not_before``, so 22:00 lands on the evening and 04:00 on the morning of
    the same night.

    Args:
        bound: a sun event name, or a local time.
        night_start: the night's dusk.
        night_end: the night's dawn.
        not_before: the earliest moment this bound may land on.

    Returns:
        The bound as a timezone-aware datetime.
    """
    if not isinstance(bound, dt.time):
        return night_start if bound == DUSK else night_end
    candidate = dt.datetime.combine(not_before.date(), bound, tzinfo=not_before.tzinfo)
    if candidate < not_before:
        candidate += dt.timedelta(days=1)
    return candidate


def active_window(
    windows: tuple[LightWindow, ...], location: LocationInfo, now: dt.datetime
) -> LightWindow | None:
    """Return the first window that contains ``now``, or None.

    Args:
        windows: the configured windows, in order.
        location: the place the sun times are computed for.
        now: a timezone-aware moment in time.

    Returns:
        The window open at ``now``, or None when none is.
    """
    night_start, night_end = night_bounds(location, now)
    for window in windows:
        start = resolve_bound(window.start, night_start, night_end, night_start)
        end = resolve_bound(window.end, night_start, night_end, start)
        if start <= now < end:
            return window
    return None


def budget_exhausted(lit_seconds: float, max_led_minutes: int) -> bool:
    """Return whether the LED has used up the current window's allowance.

    The allowance is per window, not per night: an evening window spending it
    all must not leave the early-morning window dark, since that is when the
    light is needed most.

    Args:
        lit_seconds: seconds the LED has been lit since the window opened.
        max_led_minutes: the per-window allowance; 0 means unlimited.

    Returns:
        True when no allowance is left.
    """
    if max_led_minutes <= 0:
        return False
    return lit_seconds >= max_led_minutes * SECONDS_PER_MINUTE


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
    allowed: bool, at_home: bool, battery: int | None, min_battery: int
) -> bool:
    """Decide whether the LED should be on right now.

    Args:
        allowed: whether the clock and the nightly budget allow lighting.
        at_home: whether the tracker sits in the power saving zone.
        battery: the tracker's battery level in percent, or None if unknown.
        min_battery: the level below which the LED is left off.

    Returns:
        True if the LED should be lit.
    """
    if not allowed:
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
        lit_seconds = 0.0
        window: LightWindow | None = None
        last_window: LightWindow | None = None
        last_battery: int | None = None

        while True:
            now = dt.datetime.now(settings.location.tzinfo)
            night = force_night or is_night(settings.location, now)
            window = (
                active_window(settings.light_windows, settings.location, now)
                if night
                else None
            )

            if window != last_window:
                # Each window opens with a full allowance of its own.
                lit_seconds = 0.0
                if window is not None:
                    LOGGER.info("Entering lighting window %s.", window.label)
                last_window = window

            spent = budget_exhausted(lit_seconds, settings.max_led_minutes)
            allowed = window is not None and not spent

            # The API is only queried when lighting is otherwise possible, so a
            # day, an out-of-window hour or a spent budget costs nothing.
            at_home = False
            battery: int | None = None
            if allowed:
                if settings.skip_when_home:
                    at_home = is_at_home(await tracker.pos_report(), settings)
                if not at_home:
                    battery = (await tracker.hw_info()).get("battery_level")
                    if battery != last_battery:
                        LOGGER.info("Battery at %s%%.", battery)
                        last_battery = battery

            wanted = should_light(allowed, at_home, battery, settings.min_battery)

            # The "on" command is re-sent every cycle: Tractive expires it.
            if wanted or led_on is not False:
                await tracker.set_led_active(wanted)
            if wanted != led_on:
                reason = (
                    ""
                    if wanted
                    else _off_reason(
                        night,
                        spent,
                        allowed,
                        at_home,
                        battery is not None and battery < settings.min_battery,
                    )
                )
                LOGGER.info(
                    "LED %s%s (%.0f min used in %s).",
                    "on" if wanted else "off",
                    reason,
                    lit_seconds / SECONDS_PER_MINUTE,
                    window.label if window is not None else "no window",
                )
            led_on = wanted

            delay = settings.refresh_seconds if wanted else settings.idle_poll_seconds
            if wanted:
                lit_seconds += delay
            await asyncio.sleep(delay)


def _off_reason(
    night: bool, spent: bool, allowed: bool, at_home: bool, battery_low: bool
) -> str:
    """Return a short parenthesised reason why the LED is not lit."""
    if not night:
        return " (daylight)"
    if spent:
        return " (window budget spent)"
    if not allowed:
        return " (outside every lighting window)"
    if at_home:
        return " (tracker at home)"
    if battery_low:
        return " (battery low)"
    return ""


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
