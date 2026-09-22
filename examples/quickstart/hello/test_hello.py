import unittest

from hello import greet


class GreetTests(unittest.TestCase):
    def test_greets_by_name(self) -> None:
        self.assertEqual("Hello, team!", greet("team"))

    def test_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            greet("")


if __name__ == "__main__":
    unittest.main()
