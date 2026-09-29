import io
import unittest
import unittest.mock

import numpy as np

from Tractive_NightLight import main


class TestMain(unittest.TestCase):
    def test_get_np_array(self) -> None:
        np.testing.assert_array_equal(main.get_np_array(), np.array([0]))

    @unittest.mock.patch("sys.stdout", new_callable=io.StringIO)
    def test_main(self, mock_stdout: unittest.mock.Mock) -> None:
        """Test main method by executing it and comparing terminal output to expected values."""
        main.main("world")
        # Go to beginning of output
        mock_stdout.seek(0)
        self.assertEqual(mock_stdout.readline(), "Hello world!\n")
        self.assertEqual(
            mock_stdout.readline(),
            "Here's a test that poetry dependencies are installed: [0]\n",
        )
