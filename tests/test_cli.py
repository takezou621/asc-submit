import io
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
                "keywords": {"ja": "録音,録画"},
                "subtitles": {"ja": "サブタイトル", "en-US": "Subtitle"},
                "screenshots": {"ja": [str(png)]},
                "submit": True,
            }
        )
        spec = cli.load_spec(path)
        self.assertEqual(spec["version"], "1.0.0")
        self.assertEqual(cli.spec_plan(spec), [
            "create/resolve version 1.0.0",
            "set What's New for: ja",
            "set keywords for: ja",
            "set subtitles for: en-US, ja (app-level)",
            "upload 1 screenshot(s) for ja",
            "SUBMIT for review",
        ])
        png.unlink()


class RunDryRunTests(unittest.TestCase):
    def test_dry_run_needs_no_api_key(self):
        import os

        fd, spec = tempfile.mkstemp(suffix=".json")
        Path(spec).write_text(json.dumps({"version": "1.0.0"}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ASC_")}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    rc = cli.main(["run", "6812783176", "--spec", spec, "--dry-run"])
        finally:
            Path(spec).unlink()
        self.assertEqual(rc, 0)
        self.assertIn("plan for app 6812783176", out.getvalue())


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
