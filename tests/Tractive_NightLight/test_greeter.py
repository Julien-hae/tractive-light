import unittest

from Tractive_NightLight import greeter


class TestGreeter(unittest.TestCase):
    def test_get_greeting(self) -> None:
        self.assertEqual(greeter.Greeter("World").get_greeting(), "Hello World!")
