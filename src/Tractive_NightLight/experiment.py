"""Record reproducible battery experiments on a Tractive tracker.

The production loop mixes three things that may each cost battery: reading the
tracker's reports, sending it commands, and the lit LED itself. An experiment
runs exactly one of them for a fixed time and writes everything it sees to a
JSON Lines file, so that two nights are compared from data rather than memory.

Arms:

* ``baseline``: nothing is sent to the tracker.
* ``reads``: the position and hardware reports are read at the command
  interval, as the production loop does. No command.
* ``command-off``: "LED off" is sent at the command interval. A command travels
  to the tracker while the LED stays dark.
* ``led-on``: "LED on" is sent at the command interval, as in production.

Every arm also samples the tracker at a slow, identical cadence and listens to
the push channel. Their own cost is therefore the same in all arms and cancels
out when two sessions are compared.

A session is meant for a tracker left alone outdoors and collected later, so it
has two phases. The arm runs for a fixed time, identical from one night to the
next. A passive tail follows, with sampling and listening only, until the
session is stopped: a tracker that does not move may stop reporting, and the
level it sends when it is picked up is the only reliable end of the curve.

Payloads are stored as the API returns them: the API is undocumented, and not
every field name used by :func:`summarize` has been confirmed. Only coordinates
are masked, because the repository is public.
"""

import asyncio
import contextlib
import datetime as dt
import enum
import fcntl
import itertools
import json
import logging
import os
import platform
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, TextIO

from aiotractive import Tractive

from Tractive_NightLight import night_light

LOGGER = logging.getLogger(__name__)

DEFAULT_HOURS = 8.0
DEFAULT_TAIL_HOURS = 6.0
DEFAULT_SAMPLE_SECONDS = 900.0
DEFAULT_MIN_BATTERY = 15
DEFAULT_OUTPUT_DIR = "data"
CHANNEL_RETRY_SECONDS = 10.0
STALE_REPORT_SECONDS = 900.0
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
MILLISECONDS = 1000

# How the arm of a session ended.
COMPLETED = "completed"
INTERRUPTED = "interrupted"
BATTERY_FLOOR = "battery_floor"

# How the passive tail of a session ended.
TAIL_STOPPED = "stopped"
TAIL_ELAPSED = "elapsed"
TAIL_SKIPPED = "skipped"

# Value of ``state_reason`` while the tracker saves power, as read by
# aiotractive. Not confirmed on this account.
POWER_SAVING = "POWER_SAVING"

# Keys whose value locates the tracker, hence the home.
LOCATION_KEYS = frozenset({"latlong", "address"})
REDACTED = "REDACTED"


class Arm(enum.StrEnum):
    """What an experiment does to the tracker at each command interval."""

    BASELINE = "baseline"
    READS = "reads"
    COMMAND_OFF = "command-off"
    LED_ON = "led-on"


class TrackerLike(Protocol):
    """The part of ``aiotractive``'s tracker an experiment relies on."""

    async def details(self) -> dict[str, Any]:
        """Return the tracker details, including its state."""
        ...

    async def hw_info(self) -> dict[str, Any]:
        """Return the last hardware report, including the battery level."""
        ...

    async def pos_report(self) -> dict[str, Any]:
        """Return the last position report."""
        ...

    async def set_led_active(self, active: bool) -> dict[str, Any]:
        """Send the LED command and return the API's answer."""
        ...


class EventSource(Protocol):
    """The part of ``aiotractive``'s client that streams push events."""

    def events(self) -> Any:  # an async iterator of event payloads
        """Return the stream of push events."""
        ...


@dataclass(frozen=True)
class ExperimentConfig:
    """Parameters of one experiment session.

    Attributes:
        arm: what is done to the tracker at each command interval.
        hours: how long the arm runs.
        tail_hours: how long the session keeps observing after the arm, at
            most, while waiting for the tracker to be collected.
        interval_seconds: the spacing of the arm's reads or commands.
        sample_seconds: the spacing of the observation samples, in every arm.
        min_battery: the reported level under which the arm stops early.
        note: free text stored with the session (place, weather, charge).
        output_dir: where the session file is written.
    """

    arm: Arm
    hours: float
    tail_hours: float
    interval_seconds: float
    sample_seconds: float
    min_battery: int
    note: str
    output_dir: Path

    @classmethod
    def from_env(cls, arm: Arm, default_interval_seconds: float) -> "ExperimentConfig":
        """Build the configuration from ``EXPERIMENT_*`` environment variables.

        Args:
            arm: the arm to run.
            default_interval_seconds: the command interval used when
                ``EXPERIMENT_INTERVAL_SECONDS`` is not set, normally the
                production refresh interval.

        Returns:
            The configuration of the session.
        """
        return cls(
            arm=arm,
            hours=float(os.environ.get("EXPERIMENT_HOURS") or DEFAULT_HOURS),
            tail_hours=float(
                os.environ.get("EXPERIMENT_TAIL_HOURS") or DEFAULT_TAIL_HOURS
            ),
            interval_seconds=float(
                os.environ.get("EXPERIMENT_INTERVAL_SECONDS")
                or default_interval_seconds
            ),
            sample_seconds=float(
                os.environ.get("EXPERIMENT_SAMPLE_SECONDS") or DEFAULT_SAMPLE_SECONDS
            ),
            min_battery=int(
                os.environ.get("EXPERIMENT_MIN_BATTERY") or DEFAULT_MIN_BATTERY
            ),
            note=os.environ.get("EXPERIMENT_NOTE", ""),
            output_dir=Path(os.environ.get("EXPERIMENT_DIR") or DEFAULT_OUTPUT_DIR),
        )


def redact(payload: Any) -> Any:
    """Return ``payload`` with every coordinate masked, at any depth.

    Args:
        payload: a JSON-like value, as returned by the API.

    Returns:
        A copy in which the value of each location key is replaced by a marker.
    """
    if isinstance(payload, dict):
        return {
            key: REDACTED if key in LOCATION_KEYS else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def session_filename(started: dt.datetime, arm: Arm) -> str:
    """Return the name of a session file, sortable by start time.

    Args:
        started: the moment the session starts, timezone-aware.
        arm: the arm the session runs.

    Returns:
        A name such as ``20261007T180000Z_led-on.jsonl``.
    """
    stamp = started.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{arm.value}.jsonl"


def _utcnow() -> dt.datetime:
    """Return the current time, in UTC."""
    return dt.datetime.now(dt.UTC)


class Recorder:
    """Append timestamped records to a JSON Lines stream."""

    def __init__(
        self, stream: TextIO, clock: Callable[[], dt.datetime] = _utcnow
    ) -> None:
        """Bind the recorder to a stream.

        Args:
            stream: the text stream records are appended to.
            clock: returns the current timezone-aware time.
        """
        self._stream = stream
        self._clock = clock

    def write(self, kind: str, **fields: Any) -> None:
        """Append one record, flushed at once so a crash loses nothing.

        Args:
            kind: the type of record, such as ``sample`` or ``command``.
            **fields: the content of the record; coordinates are masked.
        """
        record = {
            "t": self._clock().isoformat(timespec="seconds"),
            "kind": kind,
            **redact(fields),
        }
        self._stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._stream.flush()


def report_age_seconds(report: dict[str, Any], now: dt.datetime) -> float | None:
    """Return how old a tracker report is, or None when it carries no date.

    Args:
        report: a hardware or position report, dated by its ``time`` field.
        now: the current timezone-aware time.

    Returns:
        The age in seconds, or None.
    """
    reported = report.get("time")
    if not isinstance(reported, int | float):
        return None
    return now.timestamp() - float(reported)


def _mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, an empty one otherwise."""
    return value if isinstance(value, dict) else {}


def preflight_warnings(sample: dict[str, Any], now: dt.datetime) -> list[str]:
    """Return what makes the start of a session unfit for comparison.

    Args:
        sample: the payloads of the opening sample, keyed by call name.
        now: the current timezone-aware time.

    Returns:
        One sentence per problem found, none when the start is clean.
    """
    warnings: list[str] = []
    details, hw_info, pos_report = (
        _mapping(sample.get(name)) for name in ("details", "hw_info", "pos_report")
    )

    if str(pos_report.get("sensor_used") or "").upper() in night_light.WIFI_SENSORS:
        warnings.append(
            "The tracker locates itself by Wi-Fi: it is in its Power Saving "
            "Zone, where the network connection is paused and no command "
            "arrives. Move it out of range of the home Wi-Fi."
        )
    if details.get("state_reason") == POWER_SAVING:
        warnings.append(
            "The tracker reports that it is saving power: it may neither "
            "report nor receive commands. Move it and wait for a new report."
        )
    age = report_age_seconds(hw_info, now)
    if age is not None and age > STALE_REPORT_SECONDS:
        warnings.append(
            f"The last hardware report is {age / SECONDS_PER_MINUTE:.0f} min "
            "old: the start level is not the current one. Move the tracker "
            "and wait for a new report."
        )
    return warnings


class BatteryWatch:
    """Follow the tracker's hardware reports as they arrive.

    Each new report is logged, which is what tells someone standing next to
    the tracker that it has just reported. A level under the floor sets
    ``low``, so the arm stops before the battery is empty: a dead tracker
    sends no closing report and would leave the session without an end level.
    """

    def __init__(
        self, min_battery: int, clock: Callable[[], dt.datetime] = _utcnow
    ) -> None:
        """Start watching.

        Args:
            min_battery: the level under which ``low`` is set.
            clock: returns the current timezone-aware time.
        """
        self.low = asyncio.Event()
        self._min_battery = min_battery
        self._clock = clock
        self._last: tuple[Any, int] | None = None

    def see(self, report: Any) -> None:
        """Take one hardware report into account; a repeated one is ignored.

        Args:
            report: a hardware report from the REST API or from a push event.
        """
        if not isinstance(report, dict):
            return
        level = report.get("battery_level")
        if not isinstance(level, int):
            return
        seen = (report.get("time"), level)
        if seen == self._last:
            return
        self._last = seen

        age = report_age_seconds(report, self._clock())
        LOGGER.info(
            "Tracker reports battery at %s %%%s.",
            level,
            "" if age is None else f", {age / SECONDS_PER_MINUTE:.0f} min ago",
        )
        if level < self._min_battery:
            self.low.set()


def _git(*args: str) -> str | None:
    """Return the output of a git command run in the project, or None."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path(__file__).parent,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def code_version() -> dict[str, Any]:
    """Return what is needed to run the same code again."""
    commit = _git("rev-parse", "--short", "HEAD")
    return {
        "commit": commit,
        "dirty": None if commit is None else bool(_git("status", "--porcelain")),
        "aiotractive": metadata.version("aiotractive"),
        "python": platform.python_version(),
    }


@contextlib.contextmanager
def _exclusive(directory: Path) -> Iterator[None]:
    """Hold the experiment lock, so two sessions never share a tracker.

    Raises:
        RuntimeError: if another session already holds the lock.
    """
    with (directory / ".lock").open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            msg = "Another experiment session is already running."
            raise RuntimeError(msg) from error
        yield


def _describe(error: BaseException) -> str:
    """Return an error with its cause: aiotractive wraps the one that matters."""
    cause = error.__cause__
    return repr(error) if cause is None else f"{error!r} from {cause!r}"


async def _record(
    recorder: Recorder,
    kind: str,
    calls: dict[str, Callable[[], Awaitable[Any]]],
    **extra: Any,
) -> dict[str, Any] | None:
    """Run API calls and record their answers, or the error that stopped them.

    A failed call is data too: it is recorded and the session carries on.

    Returns:
        The answers keyed by call name, or None when a call failed.
    """
    started = time.monotonic()
    try:
        payloads = {name: await call() for name, call in calls.items()}
    except Exception as error:  # a session must survive any API failure
        recorder.write("error", during=kind, error=_describe(error), **extra)
        return None
    duration_ms = round((time.monotonic() - started) * MILLISECONDS)
    recorder.write(kind, duration_ms=duration_ms, **payloads, **extra)
    return payloads


async def _sample(
    tracker: TrackerLike, recorder: Recorder, watch: BatteryWatch
) -> dict[str, Any]:
    """Record the tracker's state, hardware report and position report.

    Returns:
        The payloads keyed by call name, empty when a call failed.
    """
    payloads = await _record(
        recorder,
        "sample",
        {
            "details": tracker.details,
            "hw_info": tracker.hw_info,
            "pos_report": tracker.pos_report,
        },
    )
    if payloads is None:
        return {}
    watch.see(payloads.get("hw_info"))
    return payloads


async def _read(tracker: TrackerLike, recorder: Recorder) -> None:
    """Read the reports the way the production loop does."""
    await _record(
        recorder,
        "read",
        {"pos_report": tracker.pos_report, "hw_info": tracker.hw_info},
    )


async def _command(tracker: TrackerLike, recorder: Recorder, active: bool) -> None:
    """Send one LED command and record the API's answer."""
    await _record(
        recorder,
        "command",
        {"response": lambda: tracker.set_led_active(active)},
        action="on" if active else "off",
    )


def _arm_action(
    arm: Arm, tracker: TrackerLike, recorder: Recorder
) -> Callable[[], Awaitable[None]] | None:
    """Return what the arm does at each command interval, or None."""
    if arm is Arm.READS:
        return lambda: _read(tracker, recorder)
    if arm is Arm.COMMAND_OFF:
        return lambda: _command(tracker, recorder, False)
    if arm is Arm.LED_ON:
        return lambda: _command(tracker, recorder, True)
    return None


async def _every(
    seconds: float, action: Callable[[], Awaitable[object]], immediately: bool
) -> None:
    """Run ``action`` at a fixed cadence that does not drift with its duration."""
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    if immediately:
        await action()
    while True:
        next_at += seconds
        await asyncio.sleep(max(0.0, next_at - loop.time()))
        await action()


async def _listen(client: EventSource, recorder: Recorder, watch: BatteryWatch) -> None:
    """Record every push event, reconnecting whenever the channel drops."""
    while True:
        try:
            async for event in client.events():
                recorder.write("event", event=event)
                if isinstance(event, dict):
                    watch.see(event.get("hardware"))
        except Exception as error:  # a session must survive a dropped channel
            recorder.write("error", during="event", error=_describe(error))
        await asyncio.sleep(CHANNEL_RETRY_SECONDS)


async def _wait(seconds: float, stop: asyncio.Event | None = None) -> str:
    """Wait out one phase of a session and say how it ended.

    A cancellation is handled here and cleared, so the closing calls of the
    session still run as in a session that reached its end.

    Args:
        seconds: how long the phase lasts.
        stop: ends the phase early when it is set.

    Returns:
        ``completed`` when the time elapsed, ``battery_floor`` when ``stop``
        was set first, ``interrupted`` when the task was cancelled.
    """
    try:
        if stop is None:
            await asyncio.sleep(seconds)
            return COMPLETED
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return COMPLETED
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        return INTERRUPTED
    return BATTERY_FLOOR


async def record_session(
    client: EventSource,
    tracker: TrackerLike,
    config: ExperimentConfig,
    recorder: Recorder,
) -> str:
    """Run one session against an open client and record it.

    The arm runs for its fixed duration, unless the battery reaches its floor
    or the task is cancelled. Unless it was cancelled, a passive tail follows:
    sampling and listening go on until the task is cancelled or the tail
    duration elapses, to catch the report the tracker sends when it is picked
    up. The session closes with a last sample and a ``session_end`` record,
    which is what tells a clean end from a crash.

    Args:
        client: the source of push events.
        tracker: the tracker under test.
        config: the parameters of the session.
        recorder: where records are written.

    Returns:
        How the arm ended: ``completed``, ``battery_floor`` or ``interrupted``.
    """
    recorder.write(
        "session_start",
        config={**asdict(config), "arm": config.arm.value},
        code=code_version(),
        host=platform.node(),
    )
    watch = BatteryWatch(config.min_battery)
    opening = await _sample(tracker, recorder, watch)
    for warning in preflight_warnings(opening, _utcnow()):
        LOGGER.warning(warning)
        recorder.write("warning", message=warning)

    observers = [
        asyncio.create_task(
            _every(
                config.sample_seconds,
                lambda: _sample(tracker, recorder, watch),
                False,
            )
        ),
        asyncio.create_task(_listen(client, recorder, watch)),
    ]
    action = _arm_action(config.arm, tracker, recorder)
    arm_task = (
        None
        if action is None
        else asyncio.create_task(_every(config.interval_seconds, action, True))
    )

    reason = await _wait(config.hours * SECONDS_PER_HOUR, watch.low)
    if arm_task is not None:
        arm_task.cancel()
        await asyncio.gather(arm_task, return_exceptions=True)
    if config.arm is Arm.LED_ON:
        await _command(tracker, recorder, False)
    recorder.write("arm_end", reason=reason)

    tail = TAIL_SKIPPED
    if reason != INTERRUPTED and config.tail_hours > 0:
        LOGGER.info(
            "Arm %s. Observing until stopped, for %s h at most.",
            reason,
            config.tail_hours,
        )
        elapsed = await _wait(config.tail_hours * SECONDS_PER_HOUR) == COMPLETED
        tail = TAIL_ELAPSED if elapsed else TAIL_STOPPED

    for task in observers:
        task.cancel()
    await asyncio.gather(*observers, return_exceptions=True)
    await _sample(tracker, recorder, watch)
    recorder.write("session_end", reason=reason, tail=tail)
    return reason


async def run(settings: night_light.Settings, config: ExperimentConfig) -> Path:
    """Run one experiment session and return the file it was recorded to.

    Args:
        settings: the runtime configuration, used for the credentials.
        config: the parameters of the session.

    Returns:
        The path of the session file.

    Raises:
        RuntimeError: if another session is already running.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / session_filename(_utcnow(), config.arm)
    with (
        _exclusive(config.output_dir),
        path.open("x", encoding="utf-8") as stream,
    ):
        LOGGER.info(
            "Experiment %s for %s h, every %ss, battery floor %s %%, recording to %s.",
            config.arm.value,
            config.hours,
            config.interval_seconds,
            config.min_battery,
            path,
        )
        async with Tractive(settings.email, settings.password) as client:
            tracker = await night_light._pick_tracker(client, settings.tracker_id)
            reason = await record_session(client, tracker, config, Recorder(stream))
    LOGGER.info("Experiment closed, arm %s, recorded to %s.", reason, path)
    return path


def execute(settings: night_light.Settings, config: ExperimentConfig) -> Path:
    """Run a session to its end, closing it cleanly on SIGTERM or Ctrl-C.

    Args:
        settings: the runtime configuration, used for the credentials.
        config: the parameters of the session.

    Returns:
        The path of the session file.
    """

    async def _until_signal() -> Path:
        task = asyncio.current_task()
        if task is not None:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        return await run(settings, config)

    return asyncio.run(_until_signal())


# --- Reading a session back ---------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """What one session shows, computed from its records alone.

    Attributes:
        arm: the arm the session ran.
        started: the time of the first record.
        end_reason: how the arm ended, ``completed``, ``battery_floor`` or
            ``interrupted``, or ``missing`` when the session stopped without
            closing (crash, reboot).
        arm_hours: how long the arm ran, when the session recorded its end.
        hours: the time between the first and the last record.
        battery_start: the level in the first hardware report.
        battery_end: the level in the last hardware report.
        battery_hours: the time between those two reports, as dated by the
            tracker when it dates them.
        drain_per_hour: percentage points lost per hour between them.
        commands: LED commands the API accepted.
        reads: production-style reads made by the ``reads`` arm.
        errors: API calls or channel connections that failed.
        warnings: problems found at the start of the session.
        hardware_reports: distinct hardware reports the tracker sent.
        position_reports: distinct position reports the tracker sent.
        longest_silence_hours: the longest stretch of the session without a
            new report from the tracker, which is what a sleeping tracker
            looks like.
        led_on_events: push events saying the LED is lit.
        led_off_events: push events saying the LED is dark.
        states: every tracker state and state reason seen.
    """

    arm: str
    started: str
    end_reason: str
    arm_hours: float | None
    hours: float
    battery_start: int | None
    battery_end: int | None
    battery_hours: float | None
    drain_per_hour: float | None
    commands: int
    reads: int
    errors: int
    warnings: int
    hardware_reports: int
    position_reports: int
    longest_silence_hours: float
    led_on_events: int
    led_off_events: int
    states: tuple[str, ...]


def load_records(path: Path) -> list[dict[str, Any]]:
    """Read a session file, skipping a line cut short by a crash.

    Args:
        path: the session file.

    Returns:
        The records, in the order they were written.
    """
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _epoch(record: dict[str, Any]) -> float:
    """Return the time a record was written, in seconds since the epoch."""
    return dt.datetime.fromisoformat(str(record["t"])).timestamp()


def _reports(record: dict[str, Any], rest_key: str, event_key: str) -> list[Any]:
    """Return the hardware or position reports a record carries."""
    event = record.get("event")
    candidates = [record.get(rest_key)]
    if isinstance(event, dict):
        candidates.append(event.get(event_key))
    return [report for report in candidates if isinstance(report, dict)]


def longest_silence_hours(start: float, end: float, reports: set[float]) -> float:
    """Return the longest stretch of a session without a new tracker report.

    Args:
        start: the start of the session, in seconds since the epoch.
        end: the end of the session, in seconds since the epoch.
        reports: the dates the tracker gave to its reports.

    Returns:
        The longest gap between two consecutive reports, the start and the end
        of the session counting as bounds, in hours.
    """
    points = sorted(
        {start, end, *(moment for moment in reports if start < moment < end)}
    )
    gaps = (later - earlier for earlier, later in itertools.pairwise(points))
    return max(gaps, default=0.0) / SECONDS_PER_HOUR


def summarize(records: list[dict[str, Any]]) -> Summary:
    """Reduce the records of one session to the figures worth comparing.

    The battery is read from hardware reports, dated by the tracker's own
    ``time`` field when it is present and by the record otherwise: a tracker
    that sleeps keeps answering with an old report, and the tracker's date is
    what shows it.

    Args:
        records: the records of one session, in order.

    Returns:
        The summary of the session.

    Raises:
        ValueError: if ``records`` is empty.
    """
    if not records:
        msg = "The session holds no record."
        raise ValueError(msg)

    battery: dict[float, int] = {}
    hardware_times: set[float] = set()
    position_times: set[float] = set()
    states: set[str] = set()
    led = {True: 0, False: 0}

    for record in records:
        for report in _reports(record, "hw_info", "hardware"):
            reported = report.get("time")
            if isinstance(reported, int | float):
                hardware_times.add(float(reported))
            level = report.get("battery_level")
            if isinstance(level, int):
                moment = (
                    float(reported)
                    if isinstance(reported, int | float)
                    else _epoch(record)
                )
                battery.setdefault(moment, level)
        for report in _reports(record, "pos_report", "position"):
            reported = report.get("time")
            if isinstance(reported, int | float):
                position_times.add(float(reported))

        details = record.get("details")
        event = record.get("event")
        for source, keys in (
            (details, ("state", "state_reason")),
            (event, ("tracker_state", "tracker_state_reason")),
        ):
            if isinstance(source, dict):
                states.update(str(source[key]) for key in keys if source.get(key))
        if isinstance(event, dict) and isinstance(event.get("led_control"), dict):
            active = event["led_control"].get("active")
            if isinstance(active, bool):
                led[active] += 1

    first, last = records[0], records[-1]
    moments = sorted(battery)
    battery_hours = (
        (moments[-1] - moments[0]) / SECONDS_PER_HOUR if len(moments) > 1 else None
    )
    start_level = battery[moments[0]] if moments else None
    end_level = battery[moments[-1]] if moments else None
    drain = (
        (start_level - end_level) / battery_hours
        if start_level is not None and end_level is not None and battery_hours
        else None
    )
    config = first.get("config")
    arm_end = next((r for r in records if r.get("kind") == "arm_end"), None)

    return Summary(
        arm=str(config.get("arm")) if isinstance(config, dict) else "unknown",
        started=str(first["t"]),
        end_reason=str(last.get("reason"))
        if last.get("kind") == "session_end"
        else "missing",
        arm_hours=None
        if arm_end is None
        else (_epoch(arm_end) - _epoch(first)) / SECONDS_PER_HOUR,
        hours=(_epoch(last) - _epoch(first)) / SECONDS_PER_HOUR,
        battery_start=start_level,
        battery_end=end_level,
        battery_hours=battery_hours,
        drain_per_hour=drain,
        commands=sum(record.get("kind") == "command" for record in records),
        reads=sum(record.get("kind") == "read" for record in records),
        errors=sum(record.get("kind") == "error" for record in records),
        warnings=sum(record.get("kind") == "warning" for record in records),
        hardware_reports=len(hardware_times),
        position_reports=len(position_times),
        longest_silence_hours=longest_silence_hours(
            _epoch(first), _epoch(last), hardware_times | position_times
        ),
        led_on_events=led[True],
        led_off_events=led[False],
        states=tuple(sorted(states)),
    )


def _figure(value: float | None, pattern: str) -> str:
    """Format a figure, or say plainly that the data does not hold it."""
    return "unknown" if value is None else pattern.format(value)


def format_summary(summary: Summary) -> str:
    """Render a summary as a few aligned lines of text.

    Args:
        summary: the summary of one session.

    Returns:
        The text, without a trailing newline.
    """
    lines = (
        ("session", f"{summary.arm}, started {summary.started}"),
        (
            "end",
            f"arm {summary.end_reason} after "
            f"{_figure(summary.arm_hours, '{:.2f} h')}, "
            f"observed {summary.hours:.2f} h",
        ),
        (
            "battery",
            f"{_figure(summary.battery_start, '{} %')} -> "
            f"{_figure(summary.battery_end, '{} %')} over "
            f"{_figure(summary.battery_hours, '{:.2f} h')} of reports, "
            f"{_figure(summary.drain_per_hour, '{:.2f} %/h')}",
        ),
        (
            "sent",
            f"{summary.commands} commands, {summary.reads} reads, "
            f"{summary.errors} errors, {summary.warnings} start warnings",
        ),
        (
            "tracker reports",
            f"{summary.hardware_reports} hardware, "
            f"{summary.position_reports} position, "
            f"longest silence {summary.longest_silence_hours:.2f} h",
        ),
        (
            "LED events",
            f"{summary.led_on_events} lit, {summary.led_off_events} dark",
        ),
        ("states", ", ".join(summary.states) or "none seen"),
    )
    return "\n".join(f"{label:<16}{text}" for label, text in lines)
