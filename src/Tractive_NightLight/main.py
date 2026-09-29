"""Entry point for the Tractive night light service."""

import argparse
import asyncio
import logging

from Tractive_NightLight import night_light

LOGGER = logging.getLogger(__name__)


def main(force_night: bool = False) -> None:
    """Start the night light loop.

    Args:
        force_night: treat every moment as night, for testing in daylight.
    """
    settings = night_light.Settings.from_env()
    LOGGER.info(
        "Starting night light for %s, refresh every %ss, battery floor %s%%.",
        settings.location.name,
        settings.refresh_seconds,
        settings.min_battery,
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
    args = parser.parse_args()
    main(force_night=args.force_night)


if __name__ == "__main__":
    cli()
