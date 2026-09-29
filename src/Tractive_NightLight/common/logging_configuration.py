"""Logging customization."""

import logging.config
import os
import time

LOGGER = logging.getLogger(__name__)


class UTCFormatter(logging.Formatter):
    """UTC formatter which converts timestamps to UTC."""

    converter = time.gmtime


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "utc": {
            "()": UTCFormatter,
            "format": "%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        }
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "utc"},
    },
    "root": {"handlers": ["console"]},
}


def configure_logger() -> None:
    """Configure logging.

    Configures a console logger which logs in UTC time with timestamps
    formatted according to ISO 8601.

    Note: loglevel defaults to logging.INFO and may be overridden by
    configuring the environment variable 'LOG_LEVEL'.
    """
    logging.config.dictConfig(LOGGING_CONFIG)
    loglevel_name = os.environ.get("LOG_LEVEL", default="INFO")
    loglevel = logging.getLevelName(loglevel_name)
    if isinstance(loglevel, str):
        LOGGER.warning(  # pylint: disable=logging-too-many-args
            "Loglevel-Name '%s' not found in loglevels. Falling back to INFO.",
            loglevel_name,
        )
        loglevel = logging.INFO

    # Set loglevel on root logger and propagate.
    logging.getLogger().setLevel(loglevel)
