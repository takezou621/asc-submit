import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asc_submit import cli


class LoadSpecTests(unittest.TestCase):
    def write(self, spec: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text(json.dumps(spec))
        return path

    def test_version_is_required(self):
        path = self.write({"whatsNew": {"ja": "x"}})
        with self.assertRaises(SystemExit):
            cli.load_spec(path)

    def test_missing_screenshot_file_is_rejected(self):
        path = self.write({"version": "1.0.0", "screenshots": {"ja": ["/no/such/file.png"]}})
        with self.assertRaises(SystemExit):
            cli.load_spec(path)

    def test_valid_spec_loads(self):
        png = Path(tempfile.mkstemp(suffix=".png")[1])
        png.write_bytes(b"\x89PNG\r\n")
        path = self.write(
            {
                "version": "1.0.0",
                "whatsNew": {"ja": "x"},
                "screenshots": {"ja": [str(png)]},
                "submit": True,
            }
        )
        spec = cli.load_spec(path)
        self.assertEqual(spec["version"], "1.0.0")
        self.assertEqual(cli.spec_plan(spec), [
            "create/resolve version 1.0.0",
            "set What's New for: ja",
            "upload 1 screenshot(s) for ja",
            "SUBMIT for review",
        ])
        png.unlink()


class ReadTextTests(unittest.TestCase):
    def test_text_and_file_are_exclusive(self):
        args = mock.Mock(text="a", file="b")
        with self.assertRaises(SystemExit):
            cli.read_text(args)

    def test_file_content_is_returned(self):
        fd, path = tempfile.mkstemp()
        Path(path).write_text("hello", encoding="utf-8")
        args = mock.Mock(text=None, file=path)
        self.assertEqual(cli.read_text(args), "hello")


if __name__ == "__main__":
    unittest.main()
