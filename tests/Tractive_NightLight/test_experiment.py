import asyncio
import datetime as dt
import io
import json
import os
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

from Tractive_NightLight import experiment
from Tractive_NightLight.experiment import Arm

START = dt.datetime(2026, 10, 7, 18, 0, 0, tzinfo=dt.UTC)
START_EPOCH = START.timestamp()


def make_config(arm: Arm = Arm.LED_ON, **overrides: Any) -> experiment.ExperimentConfig:
    defaults: dict[str, Any] = {
        "arm": arm,
        "hours": 0.2 / 3600,
        "tail_hours": 0.0,
        "interval_seconds": 0.05,
        "sample_seconds": 0.05,
        "min_battery": 0,
        "note": "",
        "output_dir": Path("data"),
    }
    defaults.update(overrides)
    return experiment.ExperimentConfig(**defaults)


class FakeTracker:
    """Answers like the API and remembers every command it was sent."""

    def __init__(self, fail_hw_info: bool = False, battery: int = 80) -> None:
        self.commands: list[bool] = []
        self.fail_hw_info = fail_hw_info
        # One report, repeated at every read, as a real tracker's is until it
        # sends the next one.
        self.hardware = {"battery_level": battery, "time": time.time()}

    async def details(self) -> dict[str, Any]:
        return {"state": "OPERATIONAL", "state_reason": None}

    async def hw_info(self) -> dict[str, Any]:
        if self.fail_hw_info:
            raise RuntimeError("boom")
        return dict(self.hardware)

    async def pos_report(self) -> dict[str, Any]:
        return {"latlong": [46.1, 6.1], "sensor_used": "GPS", "time": 2}

    async def set_led_active(self, active: bool) -> dict[str, Any]:
        self.commands.append(active)
        return {"pending": True}


class FakeClient:
    """Streams one push event, then lets the channel end."""

    def __init__(self, hardware: dict[str, Any] | None = None) -> None:
        self.hardware = hardware

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        event: dict[str, Any] = {
            "message": "tracker_status",
            "position": {"latlong": [46.1, 6.1]},
        }
        if self.hardware is not None:
            event["hardware"] = self.hardware
        yield event


async def record(
    arm: Arm,
    tracker: FakeTracker,
    client: FakeClient | None = None,
    **overrides: Any,
) -> list[dict[str, Any]]:
    stream = io.StringIO()
    await experiment.record_session(
        client or FakeClient(),
        tracker,
        make_config(arm, **overrides),
        experiment.Recorder(stream),
    )
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def kinds(records: list[dict[str, Any]]) -> list[str]:
    return [record["kind"] for record in records]


class TestRedact(unittest.TestCase):
    def test_coordinates_are_masked_at_any_depth(self) -> None:
        payload = {"event": {"position": {"latlong": [46.1, 6.1], "time": 5}}}
        self.assertEqual(
            experiment.redact(payload),
            {"event": {"position": {"latlong": "REDACTED", "time": 5}}},
        )

    def test_coordinates_inside_a_list_are_masked(self) -> None:
        self.assertEqual(
            experiment.redact([{"latlong": [1.0, 2.0]}]), [{"latlong": "REDACTED"}]
        )

    def test_other_values_are_kept_and_input_is_not_modified(self) -> None:
        payload = {"battery_level": 80, "latlong": [1.0, 2.0]}
        self.assertEqual(experiment.redact(payload)["battery_level"], 80)
        self.assertEqual(payload["latlong"], [1.0, 2.0])


class TestDescribe(unittest.TestCase):
    def test_wrapped_error_shows_its_cause(self) -> None:
        try:
            raise RuntimeError("wrapper") from TimeoutError("10 s")
        except RuntimeError as error:
            described = experiment._describe(error)
        self.assertEqual(described, "RuntimeError('wrapper') from TimeoutError('10 s')")

    def test_plain_error_is_shown_alone(self) -> None:
        self.assertEqual(experiment._describe(ValueError("x")), "ValueError('x')")


class TestSessionFilename(unittest.TestCase):
    def test_name_holds_utc_start_and_arm(self) -> None:
        self.assertEqual(
            experiment.session_filename(START, Arm.LED_ON),
            "20261007T180000Z_led-on.jsonl",
        )

    def test_local_time_is_converted_to_utc(self) -> None:
        local = dt.datetime(
            2026, 10, 7, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))
        )
        self.assertEqual(
            experiment.session_filename(local, Arm.BASELINE),
            "20261007T180000Z_baseline.jsonl",
        )


class TestConfigFromEnv(unittest.TestCase):
    def test_defaults_follow_the_production_interval(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            config = experiment.ExperimentConfig.from_env(Arm.BASELINE, 240)
        self.assertEqual(config.interval_seconds, 240)
        self.assertEqual(config.hours, 8)
        self.assertEqual(config.tail_hours, 6)
        self.assertEqual(config.sample_seconds, 900)
        self.assertEqual(config.min_battery, 15)
        self.assertEqual(config.output_dir, Path("data"))

    def test_environment_overrides_every_parameter(self) -> None:
        env = {
            "EXPERIMENT_HOURS": "1.5",
            "EXPERIMENT_TAIL_HOURS": "3",
            "EXPERIMENT_MIN_BATTERY": "25",
            "EXPERIMENT_INTERVAL_SECONDS": "120",
            "EXPERIMENT_SAMPLE_SECONDS": "600",
            "EXPERIMENT_NOTE": "garden wall, 8 C",
            "EXPERIMENT_DIR": "/tmp/sessions",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = experiment.ExperimentConfig.from_env(Arm.LED_ON, 240)
        self.assertEqual(config.hours, 1.5)
        self.assertEqual(config.tail_hours, 3)
        self.assertEqual(config.min_battery, 25)
        self.assertEqual(config.interval_seconds, 120)
        self.assertEqual(config.sample_seconds, 600)
        self.assertEqual(config.note, "garden wall, 8 C")
        self.assertEqual(config.output_dir, Path("/tmp/sessions"))

    def test_empty_values_fall_back_to_defaults(self) -> None:
        with mock.patch.dict(os.environ, {"EXPERIMENT_HOURS": ""}, clear=True):
            config = experiment.ExperimentConfig.from_env(Arm.LED_ON, 240)
        self.assertEqual(config.hours, 8)


class TestReportAge(unittest.TestCase):
    def test_age_is_counted_from_the_tracker_date(self) -> None:
        report = {"time": START_EPOCH - 120}
        self.assertEqual(experiment.report_age_seconds(report, START), 120)

    def test_undated_report_has_no_age(self) -> None:
        self.assertIsNone(experiment.report_age_seconds({"battery_level": 80}, START))


class TestPreflightWarnings(unittest.TestCase):
    def sample(self, **overrides: Any) -> dict[str, Any]:
        sample: dict[str, Any] = {
            "details": {"state": "OPERATIONAL", "state_reason": None},
            "hw_info": {"battery_level": 100, "time": START_EPOCH - 60},
            "pos_report": {"sensor_used": "GPS"},
        }
        sample.update(overrides)
        return sample

    def test_awake_tracker_outdoors_raises_nothing(self) -> None:
        self.assertEqual(experiment.preflight_warnings(self.sample(), START), [])

    def test_tracker_on_the_home_wifi_is_flagged(self) -> None:
        sample = self.sample(pos_report={"sensor_used": "KNOWN_WIFI"})
        warnings = experiment.preflight_warnings(sample, START)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Power Saving Zone", warnings[0])

    def test_tracker_saving_power_is_flagged(self) -> None:
        sample = self.sample(details={"state_reason": "POWER_SAVING"})
        warnings = experiment.preflight_warnings(sample, START)
        self.assertEqual(len(warnings), 1)
        self.assertIn("saving power", warnings[0])

    def test_old_hardware_report_is_flagged_with_its_age(self) -> None:
        sample = self.sample(hw_info={"battery_level": 90, "time": START_EPOCH - 3600})
        warnings = experiment.preflight_warnings(sample, START)
        self.assertEqual(len(warnings), 1)
        self.assertIn("60 min old", warnings[0])

    def test_failed_opening_sample_raises_nothing(self) -> None:
        self.assertEqual(experiment.preflight_warnings({}, START), [])


class TestBatteryWatch(unittest.TestCase):
    def test_each_new_report_is_logged_once(self) -> None:
        watch = experiment.BatteryWatch(15, clock=lambda: START)
        report = {"battery_level": 80, "time": START_EPOCH - 120}
        with self.assertLogs(experiment.LOGGER, "INFO") as logs:
            watch.see(report)
            watch.see(report)
            watch.see({"battery_level": 79, "time": START_EPOCH})
        self.assertEqual(len(logs.output), 2)
        self.assertIn("battery at 80 %, 2 min ago", logs.output[0])
        self.assertFalse(watch.low.is_set())

    def test_level_under_the_floor_sets_low(self) -> None:
        watch = experiment.BatteryWatch(15, clock=lambda: START)
        with self.assertLogs(experiment.LOGGER, "INFO"):
            watch.see({"battery_level": 15})
            self.assertFalse(watch.low.is_set())
            watch.see({"battery_level": 14})
        self.assertTrue(watch.low.is_set())

    def test_payload_without_a_level_is_ignored(self) -> None:
        watch = experiment.BatteryWatch(15)
        with self.assertNoLogs(experiment.LOGGER, "INFO"):
            watch.see(None)
            watch.see({"time": START_EPOCH})
        self.assertFalse(watch.low.is_set())


class TestLongestSilence(unittest.TestCase):
    def test_gap_between_reports_is_measured_in_hours(self) -> None:
        reports = {START_EPOCH + 3600, START_EPOCH + 7 * 3600}
        self.assertEqual(
            experiment.longest_silence_hours(
                START_EPOCH, START_EPOCH + 8 * 3600, reports
            ),
            6,
        )

    def test_session_without_a_report_is_silent_from_start_to_end(self) -> None:
        self.assertEqual(
            experiment.longest_silence_hours(START_EPOCH, START_EPOCH + 7200, set()),
            2,
        )

    def test_reports_dated_outside_the_session_are_left_out(self) -> None:
        reports = {START_EPOCH - 600, START_EPOCH + 9000}
        self.assertEqual(
            experiment.longest_silence_hours(START_EPOCH, START_EPOCH + 3600, reports),
            1,
        )


class TestRecorder(unittest.TestCase):
    def test_record_is_one_timestamped_json_line_without_coordinates(self) -> None:
        stream = io.StringIO()
        recorder = experiment.Recorder(stream, clock=lambda: START)
        recorder.write("sample", pos_report={"latlong": [46.1, 6.1], "time": 5})
        self.assertEqual(
            json.loads(stream.getvalue()),
            {
                "t": "2026-10-07T18:00:00+00:00",
                "kind": "sample",
                "pos_report": {"latlong": "REDACTED", "time": 5},
            },
        )
        self.assertTrue(stream.getvalue().endswith("\n"))


class TestLoadRecords(unittest.TestCase):
    def test_line_cut_short_by_a_crash_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text('{"kind": "sample"}\n{"kind": "comm', encoding="utf-8")
            self.assertEqual(experiment.load_records(path), [{"kind": "sample"}])


def at(hours: float, kind: str, **fields: Any) -> dict[str, Any]:
    moment = START + dt.timedelta(hours=hours)
    return {"t": moment.isoformat(timespec="seconds"), "kind": kind, **fields}


def hw(hours: float, level: int) -> dict[str, Any]:
    return {"battery_level": level, "time": START_EPOCH + hours * 3600}


class TestSummarize(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            at(0, "session_start", config={"arm": "led-on"}),
            at(
                0,
                "sample",
                hw_info=hw(0, 100),
                pos_report={"time": 10},
                details={"state": "OPERATIONAL", "state_reason": None},
            ),
            at(0, "warning", message="The last hardware report is 60 min old."),
            at(0, "command", action="on", response={"pending": True}),
            at(0.1, "event", event={"led_control": {"active": True}}),
            at(4, "event", event={"hardware": hw(4, 80), "position": {"time": 20}}),
            at(4, "command", action="on", response={"pending": True}),
            at(5, "error", during="command", error="TractiveError()"),
            at(6, "arm_end", reason="completed"),
            # The tracker went to sleep: the report is the one from hour 4.
            at(
                8,
                "sample",
                hw_info=hw(4, 80),
                pos_report={"time": 20},
                details={"state": "OPERATIONAL", "state_reason": "POWER_SAVING"},
            ),
            at(8, "session_end", reason="completed"),
        ]

    def test_battery_is_dated_by_the_tracker_not_by_the_sample(self) -> None:
        summary = experiment.summarize(self.records)
        self.assertEqual(summary.battery_start, 100)
        self.assertEqual(summary.battery_end, 80)
        self.assertEqual(summary.battery_hours, 4)
        self.assertEqual(summary.drain_per_hour, 5)

    def test_repeated_reports_are_counted_once(self) -> None:
        summary = experiment.summarize(self.records)
        self.assertEqual(summary.hardware_reports, 2)
        self.assertEqual(summary.position_reports, 2)

    def test_activity_and_states_are_counted(self) -> None:
        summary = experiment.summarize(self.records)
        self.assertEqual(summary.arm, "led-on")
        self.assertEqual(summary.commands, 2)
        self.assertEqual(summary.reads, 0)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.led_on_events, 1)
        self.assertEqual(summary.led_off_events, 0)
        self.assertEqual(summary.states, ("OPERATIONAL", "POWER_SAVING"))
        self.assertEqual(summary.warnings, 1)
        self.assertEqual(summary.arm_hours, 6)
        self.assertEqual(summary.hours, 8)
        self.assertEqual(summary.end_reason, "completed")

    def test_sleeping_tracker_shows_as_a_long_silence(self) -> None:
        summary = experiment.summarize(self.records)
        self.assertEqual(summary.longest_silence_hours, 4)

    def test_session_cut_before_the_arm_ended_has_no_arm_duration(self) -> None:
        summary = experiment.summarize(self.records[:5])
        self.assertIsNone(summary.arm_hours)
        self.assertEqual(summary.end_reason, "missing")

    def test_session_without_an_end_record_is_flagged(self) -> None:
        summary = experiment.summarize(self.records[:-1])
        self.assertEqual(summary.end_reason, "missing")

    def test_undated_reports_fall_back_to_the_record_time(self) -> None:
        records = [
            at(0, "sample", hw_info={"battery_level": 90}),
            at(2, "sample", hw_info={"battery_level": 84}),
        ]
        summary = experiment.summarize(records)
        self.assertEqual(summary.battery_hours, 2)
        self.assertEqual(summary.drain_per_hour, 3)
        self.assertEqual(summary.hardware_reports, 0)

    def test_single_report_gives_no_drain(self) -> None:
        summary = experiment.summarize([at(0, "sample", hw_info=hw(0, 90))])
        self.assertEqual(summary.battery_start, 90)
        self.assertIsNone(summary.battery_hours)
        self.assertIsNone(summary.drain_per_hour)

    def test_session_without_battery_data_is_reported_as_unknown(self) -> None:
        summary = experiment.summarize([at(0, "session_start", config={})])
        self.assertIsNone(summary.battery_start)
        self.assertIn("unknown", experiment.format_summary(summary))

    def test_empty_session_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            experiment.summarize([])

    def test_formatted_summary_names_the_figures(self) -> None:
        text = experiment.format_summary(experiment.summarize(self.records))
        self.assertIn("100 % -> 80 % over 4.00 h of reports, 5.00 %/h", text)
        self.assertIn("2 commands, 0 reads, 1 errors, 1 start warnings", text)
        self.assertIn("arm completed after 6.00 h, observed 8.00 h", text)
        self.assertIn("longest silence 4.00 h", text)
        self.assertIn("OPERATIONAL, POWER_SAVING", text)


class TestRecordSession(unittest.IsolatedAsyncioTestCase):
    async def test_led_arm_commands_at_once_then_turns_the_led_off(self) -> None:
        tracker = FakeTracker()
        records = await record(Arm.LED_ON, tracker)
        sequence = [kind for kind in kinds(records) if kind != "event"]
        self.assertEqual(sequence[:3], ["session_start", "sample", "command"])
        self.assertEqual(sequence[-4:], ["command", "arm_end", "sample", "session_end"])
        self.assertGreaterEqual(tracker.commands.count(True), 3)
        self.assertEqual(tracker.commands[-1], False)
        self.assertEqual(tracker.commands.count(False), 1)
        self.assertEqual(records[-1]["reason"], "completed")

    async def test_command_off_arm_never_lights_the_led(self) -> None:
        tracker = FakeTracker()
        records = await record(Arm.COMMAND_OFF, tracker)
        self.assertGreaterEqual(len(tracker.commands), 3)
        self.assertNotIn(True, tracker.commands)
        self.assertIn("command", kinds(records))

    async def test_baseline_arm_sends_nothing(self) -> None:
        tracker = FakeTracker()
        records = await record(Arm.BASELINE, tracker)
        self.assertEqual(tracker.commands, [])
        self.assertNotIn("read", kinds(records))
        self.assertGreaterEqual(kinds(records).count("sample"), 3)

    async def test_reads_arm_reads_without_commanding(self) -> None:
        tracker = FakeTracker()
        records = await record(Arm.READS, tracker)
        self.assertEqual(tracker.commands, [])
        self.assertGreaterEqual(kinds(records).count("read"), 3)

    async def test_push_events_are_recorded_without_coordinates(self) -> None:
        records = await record(Arm.BASELINE, FakeTracker())
        events = [record for record in records if record["kind"] == "event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"]["position"]["latlong"], "REDACTED")

    async def test_session_start_holds_what_is_needed_to_run_it_again(self) -> None:
        records = await record(Arm.LED_ON, FakeTracker(), note="garden wall")
        start = records[0]
        self.assertEqual(start["config"]["arm"], "led-on")
        self.assertEqual(start["config"]["interval_seconds"], 0.05)
        self.assertEqual(start["config"]["note"], "garden wall")
        self.assertEqual(
            set(start["code"]), {"commit", "dirty", "aiotractive", "python"}
        )

    async def test_failing_call_is_recorded_and_the_session_goes_on(self) -> None:
        tracker = FakeTracker(fail_hw_info=True)
        records = await record(Arm.LED_ON, tracker)
        errors = [record for record in records if record["kind"] == "error"]
        self.assertGreaterEqual(len(errors), 2)
        self.assertEqual(errors[0]["during"], "sample")
        self.assertIn("boom", errors[0]["error"])
        self.assertGreaterEqual(tracker.commands.count(True), 3)
        self.assertEqual(records[-1]["kind"], "session_end")

    async def test_interrupted_session_is_closed_cleanly(self) -> None:
        tracker = FakeTracker()
        stream = io.StringIO()
        task = asyncio.create_task(
            experiment.record_session(
                FakeClient(),
                tracker,
                make_config(Arm.LED_ON, hours=1),
                experiment.Recorder(stream),
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        self.assertEqual(await task, "interrupted")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(records[-1]["kind"], "session_end")
        self.assertEqual(records[-1]["reason"], "interrupted")
        self.assertEqual(records[-1]["tail"], "skipped")
        self.assertEqual(records[-2]["kind"], "sample")
        self.assertEqual(tracker.commands[-1], False)

    async def test_tail_observes_without_commanding_until_stopped(self) -> None:
        tracker = FakeTracker()
        stream = io.StringIO()
        task = asyncio.create_task(
            experiment.record_session(
                FakeClient(),
                tracker,
                make_config(Arm.LED_ON, tail_hours=1),
                experiment.Recorder(stream),
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        self.assertEqual(await task, "completed")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        tail = kinds(records)[kinds(records).index("arm_end") + 1 :]
        self.assertNotIn("command", tail)
        self.assertGreaterEqual(tail.count("sample"), 3)
        self.assertEqual(records[-1]["reason"], "completed")
        self.assertEqual(records[-1]["tail"], "stopped")
        self.assertEqual(tracker.commands[-1], False)

    async def test_tail_ends_by_itself_when_nobody_stops_it(self) -> None:
        records = await record(Arm.BASELINE, FakeTracker(), tail_hours=0.2 / 3600)
        self.assertEqual(records[-1]["reason"], "completed")
        self.assertEqual(records[-1]["tail"], "elapsed")

    async def test_battery_under_the_floor_ends_the_arm_early(self) -> None:
        tracker = FakeTracker(battery=10)
        records = await record(Arm.LED_ON, tracker, hours=1, min_battery=15)
        self.assertEqual(records[-1]["reason"], "battery_floor")
        arm_end = next(r for r in records if r["kind"] == "arm_end")
        self.assertEqual(arm_end["reason"], "battery_floor")
        self.assertEqual(tracker.commands[-1], False)

    async def test_battery_floor_is_also_read_from_push_events(self) -> None:
        client = FakeClient(hardware={"battery_level": 5, "time": time.time()})
        records = await record(
            Arm.BASELINE, FakeTracker(), client, hours=1, min_battery=15
        )
        self.assertEqual(records[-1]["reason"], "battery_floor")

    async def test_unfit_start_is_recorded_as_a_warning(self) -> None:
        class TrackerAtHome(FakeTracker):
            async def pos_report(self) -> dict[str, Any]:
                return {"sensor_used": "KNOWN_WIFI", "time": 2}

        with self.assertLogs(experiment.LOGGER, "WARNING"):
            records = await record(Arm.BASELINE, TrackerAtHome())
        warnings = [r for r in records if r["kind"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Power Saving Zone", warnings[0]["message"])
