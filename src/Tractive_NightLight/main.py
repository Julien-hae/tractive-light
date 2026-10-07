"""Entry point for the Tractive night light service."""

import argparse
import asyncio
import logging
from pathlib import Path

from Tractive_NightLight import experiment, night_light

LOGGER = logging.getLogger(__name__)


def main(force_night: bool = False, do_inspect: bool = False) -> None:
    """Start the night light loop, or dump the raw tracker payloads.

    Args:
        force_night: treat every moment as night, for testing in daylight.
        do_inspect: print the tracker payloads and exit.
    """
    settings = night_light.Settings.from_env()
    if do_inspect:
        asyncio.run(night_light.inspect(settings))
        return

    LOGGER.info(
        "Starting night light for %s, refresh every %ss, battery floor %s%%, "
        "skip when home: %s.",
        settings.location.name,
        settings.refresh_seconds,
        settings.min_battery,
        settings.skip_when_home,
    )
    asyncio.run(night_light.run(settings, force_night=force_night))


def cli() -> None:
    """Cli-Entrypoint."""
    parser = argparse.ArgumentParser(
        description="Keep a Tractive tracker's LED lit during the night."
    )
    parser.add_argument(
        "--force-night",
        action="store_true",
        help="Treat the current time as night (useful to test during the day).",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print the raw tracker payloads and exit.",
    )
    parser.add_argument(
        "--experiment",
        choices=[arm.value for arm in experiment.Arm],
        help="Run one battery experiment session and record it to a file.",
    )
    parser.add_argument(
        "--summarize",
        nargs="+",
        type=Path,
        metavar="FILE",
        help="Print the summary of recorded experiment sessions and exit.",
    )
    args = parser.parse_args()

    if args.summarize:
        for path in args.summarize:
            summary = experiment.summarize(experiment.load_records(path))
            print(f"{path}\n{experiment.format_summary(summary)}\n")
        return
    if args.experiment:
        settings = night_light.Settings.from_env()
        experiment.execute(
            settings,
            experiment.ExperimentConfig.from_env(
                experiment.Arm(args.experiment), settings.refresh_seconds
            ),
        )
        return

    main(force_night=args.force_night, do_inspect=args.inspect)


if __name__ == "__main__":
    cli()
