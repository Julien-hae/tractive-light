"""The great Greeter module."""


class Greeter:
    """Class for generating greetings."""

    def __init__(self, name: str) -> None:
        """Initialize a Greeter object."""
        self.name: str = name

    def get_greeting(self) -> str:
        """Return a greeting."""
        return f"Hello {self.name}!"
