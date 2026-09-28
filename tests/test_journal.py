import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asc_submit import journal as j


def recorded_run(runs_dir, command="run", title="test run", fail=False):
    """A finished two-step run: step one succeeds, step two fails or succeeds."""
    jr = j.RunJournal(command=command, title=title, runs_dir=runs_dir)
    jr.start()
    with jr.step("first step"):
        jr.log("line one")
        jr.log("line two")
    try:
        with jr.step("second step"):
            jr.log("line three")
            if fail:
                raise SystemExit("boom")
    except SystemExit:
        pass
    if not fail:
        jr.succeed()
    return jr


class RunJournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_run_id_is_url_safe_and_sortable(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        self.assertRegex(jr.run_id, r"^\d{8}-\d{6}-[0-9a-f]{4}$")

    def test_successful_lifecycle(self):
        jr = recorded_run(self.dir)
        state = j.read_run(jr.run_id, self.dir)
        self.assertEqual(state["status"], "success")
        self.assertEqual([s["status"] for s in state["steps"]], ["success", "success"])
        self.assertIsNone(state["error"])
        self.assertIsNotNone(state["finished_at"])

    def test_failed_step_fails_the_run_and_closes_the_step(self):
        jr = recorded_run(self.dir, fail=True)
        state = j.read_run(jr.run_id, self.dir)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "boom")
        self.assertEqual(state["steps"][1]["status"], "failed")
        self.assertEqual(state["steps"][1]["error"], "boom")
        # the earlier step keeps its success status
        self.assertEqual(state["steps"][0]["status"], "success")

    def test_step_exception_propagates(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        jr.start()
        with self.assertRaises(SystemExit):
            with jr.step("bad"):
                raise SystemExit("nope")
        self.assertEqual(jr.state["status"], "failed")

    def test_finish_is_idempotent(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        jr.start()
        jr.fail("first")
        jr.succeed()  # ignored
        self.assertEqual(jr.state["status"], "failed")
        self.assertEqual(jr.state["error"], "first")

    def test_cancel_closes_running_steps(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        jr.start()
        entry = jr.state["steps"]
        with jr.step("long"):
            jr.log("working")
            jr.cancel("interrupted")
            self.assertEqual(entry[0]["status"], "cancelled")

    def test_log_ranges_cover_each_step(self):
        jr = recorded_run(self.dir)
        text, offset, size = j.read_log(jr.run_id, self.dir)
        self.assertEqual(offset, size)
        self.assertIn("line one", text)
        state = j.read_run(jr.run_id, self.dir)
        first = state["steps"][0]
        slice_, _, _ = j.read_log(jr.run_id, self.dir, first["log_start"], first["log_end"])
        self.assertEqual(slice_, "line one\nline two\n")

    def test_multibyte_log_lines_keep_offsets_consistent(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        jr.start()
        with jr.step("日本語ステップ"):
            jr.log("録音,録画 — 文字起こし")
        state = j.read_run(jr.run_id, self.dir)
        step = state["steps"][0]
        slice_, _, _ = j.read_log(jr.run_id, self.dir, step["log_start"], step["log_end"])
        self.assertEqual(slice_, "録音,録画 — 文字起こし\n")

    def test_log_prints_to_stdout(self):
        jr = j.RunJournal(command="run", title="t", runs_dir=self.dir)
        jr.start()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            jr.log("hello")
        self.assertIn("hello", out.getvalue())


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_list_runs_newest_first_and_skips_junk(self):
        (self.dir / "not-a-run").mkdir()
        (self.dir / "junk.txt").write_text("x")
        first = j.RunJournal(command="run", title="older", runs_dir=self.dir)
        first.start()
        first.succeed()
        second = j.RunJournal(command="upload", title="newer", runs_dir=self.dir)
        second.start()
        second.succeed()
        runs = j.list_runs(self.dir)
        self.assertEqual([r["id"] for r in runs], [second.run_id, first.run_id])
        self.assertEqual(runs[0]["command"], "upload")
        self.assertNotIn("steps", runs[0])

    def test_read_run_rejects_malformed_ids(self):
        self.assertIsNone(j.read_run("../../etc", self.dir))
        self.assertIsNone(j.read_run("deadbeef", self.dir))
        self.assertIsNone(j.read_run("20260928-123456-zzzz", self.dir))

    def test_read_run_of_missing_dir_is_none(self):
        self.assertIsNone(j.read_run("20260928-123456-abcd", self.dir))

    def test_read_log_of_missing_run_is_empty(self):
        text, offset, size = j.read_log("20260928-123456-abcd", self.dir)
        self.assertEqual((text, offset, size), ("", 0, 0))

    def test_summary_has_duration(self):
        jr = recorded_run(self.dir)
        summary = j.list_runs(self.dir)[0]
        self.assertGreaterEqual(summary["duration_seconds"], 0)


class NullJournalTests(unittest.TestCase):
    def test_step_is_a_noop_context_manager(self):
        null = j.NullJournal()
        with null.step("anything") as entry:
            self.assertIsNone(entry)

    def test_log_prints_without_files(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            j.NullJournal().log("plain")
        self.assertEqual(out.getvalue(), "plain\n")

    def test_finishers_are_noops(self):
        null = j.NullJournal()
        null.succeed()
        null.fail("x")
        null.cancel()


if __name__ == "__main__":
    unittest.main()
