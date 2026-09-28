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


class RunsLogsCommandTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from asc_submit import journal as journal_mod

        self.runs_dir = tempfile.mkdtemp()
        jr = journal_mod.RunJournal(command="run", title="6812783176 → 1.0.0", runs_dir=self.runs_dir)
        jr.start()
        with jr.step("create/resolve version 1.0.0"):
            jr.log("version 1.0.0 ready (V1)")
        jr.succeed()
        self.run_id = jr.run_id

    def test_runs_list(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["runs", "--runs-dir", self.runs_dir])
        self.assertEqual(rc, 0)
        self.assertIn(self.run_id, out.getvalue())
        self.assertIn("success", out.getvalue())

    def test_runs_detail_shows_steps(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["runs", self.run_id, "--runs-dir", self.runs_dir])
        self.assertEqual(rc, 0)
        self.assertIn("create/resolve version 1.0.0", out.getvalue())
        self.assertIn("[✓]", out.getvalue())

    def test_runs_detail_unknown_id_exits(self):
        with self.assertRaises(SystemExit):
            cli.main(["runs", "20260928-123456-abcd", "--runs-dir", self.runs_dir])

    def test_logs_prints_output(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["logs", self.run_id, "--runs-dir", self.runs_dir])
        self.assertEqual(rc, 0)
        self.assertIn("version 1.0.0 ready (V1)", out.getvalue())

    def test_logs_follow_returns_when_run_is_finished(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["logs", self.run_id, "--runs-dir", self.runs_dir, "--follow"])
        self.assertEqual(rc, 0)
        self.assertIn("version 1.0.0 ready (V1)", out.getvalue())


class RunSpecJournalTests(unittest.TestCase):
    """run_spec records every phase as a journal step."""

    def setUp(self):
        import tempfile

        from asc_submit import journal as journal_mod

        self.runs_dir = tempfile.mkdtemp()
        client = mock.Mock()
        client.get_all.return_value = []  # no versions, no builds listed
        client.post.return_value = {"data": {"id": "V1", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}}}
        self.client = client
        self.journal = journal_mod.RunJournal(
            command="run", title="6812783176 → 1.0.0", runs_dir=self.runs_dir
        )
        self.journal.start()

    def test_minimal_spec_records_create_step_and_succeeds(self):
        cli.run_spec(self.client, "6812783176", {"version": "1.0.0"}, False, True, self.journal)
        self.journal.succeed()
        from asc_submit import journal as journal_mod

        state = journal_mod.read_run(self.journal.run_id, self.runs_dir)
        self.assertEqual(state["status"], "success")
        self.assertEqual([s["name"] for s in state["steps"]], ["create/resolve version 1.0.0"])
        self.assertEqual(state["steps"][0]["status"], "success")

    def test_error_in_a_step_fails_the_run(self):
        self.client.post.side_effect = RuntimeError("api down")
        with self.assertRaises(RuntimeError):
            cli.run_spec(self.client, "6812783176", {"version": "1.0.0"}, False, True, self.journal)
        from asc_submit import journal as journal_mod

        state = journal_mod.read_run(self.journal.run_id, self.runs_dir)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["steps"][0]["status"], "failed")
        self.assertIn("api down", state["error"])
