"""Main Description."""

import argparse
import logging

import numpy as np
import numpy.typing as npt

from Tractive_NightLight import greeter

LOGGER = logging.getLogger(__name__)


def get_np_array() -> npt.NDArray[np.int_]:
    """Return a numpy array."""
    return np.array([0])


def main(word: str) -> None:
    """Main entry point for the application."""
    LOGGER.info("Executing entrypoint.")
    print(greeter.Greeter(word).get_greeting())
    print(f"Here's a test that poetry dependencies are installed: {get_np_array()}")


def cli() -> None:
    """Cli-Entrypoint."""
    parser = argparse.ArgumentParser(description="Demo argument")
    parser.add_argument("-w", "--word", required=True)
    args = parser.parse_args()
    main(args.word)


if __name__ == "__main__":
    cli()
