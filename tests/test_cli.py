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


class DeclinedSubmissionTests(unittest.TestCase):
    """Answering no to the submit prompt records a cancelled run, not success."""

    def setUp(self):
        import tempfile

        from asc_submit import journal as journal_mod

        self.runs_dir = tempfile.mkdtemp()
        self.journal_mod = journal_mod
        client = mock.Mock()
        client.get_all.return_value = []
        client.post.return_value = {"data": {"id": "V1", "attributes": {}}}
        self.client = client
        fd, self.spec_path = tempfile.mkstemp(suffix=".json")
        Path(self.spec_path).write_text(json.dumps({"version": "1.0.0"}))

    def tearDown(self):
        Path(self.spec_path).unlink()

    def test_run_spec_returns_declined_and_cancels_the_step(self):
        jr = self.journal_mod.RunJournal(command="run", title="t", runs_dir=self.runs_dir)
        jr.start()
        stdin = mock.Mock(isatty=lambda: True)
        with mock.patch("sys.stdin", stdin), mock.patch("builtins.input", return_value="n"):
            outcome = cli.run_spec(self.client, "6812783176", {"version": "1.0.0"}, True, False, jr)
        self.assertEqual(outcome, "declined")
        self.assertEqual(jr.state["steps"][-1]["name"], "submit for review")
        self.assertEqual(jr.state["steps"][-1]["status"], "cancelled")

    def test_cmd_run_marks_the_run_cancelled_on_decline(self):
        args = mock.Mock()
        args.spec = self.spec_path
        args.app = "6812783176"
        args.dry_run = False
        args.submit = False
        args.yes = True
        args.runs_dir = self.runs_dir
        with mock.patch.object(cli, "run_spec", return_value="declined"):
            with mock.patch.object(cli, "build_client", return_value=object()):
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    cli.cmd_run(args)
        state = self.journal_mod.list_runs(self.runs_dir)[0]
        self.assertEqual(state["status"], "cancelled")
        self.assertIn("declined", state["error"])


class FormatTimeTests(unittest.TestCase):
    def test_microseconds_and_offset_are_dropped(self):
        self.assertEqual(
            cli._fmt_time("2026-09-28T14:53:23.044409+09:00"),
            "2026-09-28 14:53:23",
        )

    def test_none_and_garbage(self):
        self.assertEqual(cli._fmt_time(None), "")
        self.assertEqual(cli._fmt_time("not a date"), "not a date")


class ValidateSpecTests(unittest.TestCase):
    def lint(self, spec):
        return cli.validate_spec(spec)

    def test_clean_spec_has_no_findings(self):
        errors, warnings = self.lint(
            {
                "version": "1.0.0",
                "build": "8",
                "whatsNew": {"ja": "安定性を改善しました。", "en-US": "Stability improvements."},
                "keywords": {"ja": "録音,録画", "en-US": "recorder,meeting"},
                "subtitles": {"ja": "録って文字起こし"},
                "_comment": "underscore keys are the comment convention",
            }
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_placeholder_markers_are_errors(self):
        for field, value in [
            ("whatsNew", {"ja": "TODO: 直す"}),
            ("descriptions", {"ja": "Lorem ipsum dolor sit amet"}),
            ("subtitles", {"ja": "<app name> here"}),
            ("reviewNotes", "TBD how to test"),
        ]:
            errors, _ = self.lint({"version": "1.0.0", field: value})
            self.assertTrue(errors, field)

    def test_unknown_key_with_typo_suggests_the_real_one(self):
        errors, _ = self.lint({"version": "1.0.0", "whatsnew": {"ja": "x"}})
        self.assertTrue(any("did you mean 'whatsNew'" in e for e in errors))

    def test_wrong_type_is_an_error(self):
        errors, _ = self.lint({"version": "1.0.0", "whatsNew": "one line, not a locale map"})
        self.assertTrue(any(e.startswith("whatsNew:") for e in errors))

    def test_numeric_build_is_an_error(self):
        errors, _ = self.lint({"version": "1.0.0", "build": 8})
        self.assertTrue(any(e.startswith("build:") for e in errors))

    def test_invalid_platform_and_release_type(self):
        errors, _ = self.lint({"version": "1.0.0", "platform": "OS_X", "releaseType": "WHENEVER"})
        self.assertTrue(any(e.startswith("platform:") for e in errors))
        self.assertTrue(any(e.startswith("releaseType:") for e in errors))

    def test_limits_mirror_the_write_time_checks(self):
        errors, _ = self.lint(
            {"version": "1.0.0", "keywords": {"ja": "字" * 101}, "subtitles": {"ja": "字" * 31}}
        )
        self.assertTrue(any("100-character limit" in e for e in errors))
        self.assertTrue(any("30-character limit" in e for e in errors))

    def test_keyword_audit_reports_duplicates_and_spaces_as_warnings(self):
        _, warnings = self.lint({"version": "1.0.0", "keywords": {"ja": "録音, 録画,録音,"}})
        self.assertTrue(any("duplicate keyword" in w for w in warnings))
        self.assertTrue(any("contains a space" in w for w in warnings))
        self.assertTrue(any("empty keyword" in w for w in warnings))

    def test_submit_without_build_is_a_warning(self):
        errors, warnings = self.lint({"version": "1.0.0", "submit": True})
        self.assertEqual(errors, [])
        self.assertTrue(any("attached build" in w for w in warnings))

    def test_missing_screenshot_file_is_an_error(self):
        errors, _ = self.lint({"version": "1.0.0", "screenshots": {"ja": ["/no/such/file.png"]}})
        self.assertTrue(any("file not found" in e for e in errors))


class ValidateCommandTests(unittest.TestCase):
    def write(self, spec_text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text(spec_text, encoding="utf-8")
        return path

    def test_clean_spec_exits_zero(self):
        path = self.write(json.dumps({"version": "1.0.0"}))
        try:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cli.main(["validate", "--spec", path])
        finally:
            Path(path).unlink()
        self.assertEqual(rc, 0)
        self.assertIn("no issues found", out.getvalue())

    def test_errors_exit_nonzero(self):
        path = self.write(json.dumps({"version": "1.0.0", "whatsNew": {"ja": "TODO"}}))
        try:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["validate", "--spec", path])
        finally:
            Path(path).unlink()
        self.assertEqual(ctx.exception.code, 1)

    def test_broken_json_exits_with_message(self):
        path = self.write("{not json")
        try:
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["validate", "--spec", path])
        finally:
            Path(path).unlink()
        self.assertIn("cannot read spec", str(ctx.exception))

    def test_needs_no_api_key(self):
        import os

        path = self.write(json.dumps({"version": "1.0.0"}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ASC_")}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    rc = cli.main(["validate", "--spec", path])
        finally:
            Path(path).unlink()
        self.assertEqual(rc, 0)


class RunLintGateTests(unittest.TestCase):
    """`run` lints the spec before anything else — no API key needed to fail."""

    def test_placeholder_spec_is_rejected_before_client_construction(self):
        import os

        fd, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text(json.dumps({"version": "1.0.0", "whatsNew": {"ja": "TODO 書く"}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ASC_")}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(cli, "build_client") as build:
                    with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                        with self.assertRaises(SystemExit):
                            cli.main(["run", "6812783176", "--spec", path])
        finally:
            Path(path).unlink()
        self.assertFalse(build.called)
        self.assertIn("placeholder", err.getvalue())

    def test_warnings_print_but_do_not_stop_the_run(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text(
            json.dumps({"version": "1.0.0", "submit": True})  # build missing → warning
        )
        args = mock.Mock()
        args.spec = path
        args.app = "6812783176"
        args.dry_run = True
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.cmd_run(args)
        Path(path).unlink()
        self.assertIn("warning:", out.getvalue())
        self.assertIn("plan for app 6812783176", out.getvalue())


class WaitCommandTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from asc_submit import journal as journal_mod

        self.journal_mod = journal_mod
        self.runs_dir = tempfile.mkdtemp()

    def _args(self, **over):
        values = {"app": "6812783176", "version": "0.9.0", "timeout": 60, "interval": 0, "webhook": None, "runs_dir": self.runs_dir}
        values.update(over)
        return mock.Mock(**values)

    def _version(self, state):
        return {"id": "V1", "attributes": {"versionString": "0.9.0", "appStoreState": state}}

    def test_approval_records_a_successful_run(self):
        args = self._args()
        with mock.patch.object(cli.flows, "wait_for_review", return_value=self._version("READY_FOR_SALE")):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_wait(mock.Mock(), args)
        state = self.journal_mod.list_runs(self.runs_dir)[0]
        self.assertEqual(state["status"], "success")
        self.assertIn("READY_FOR_SALE", out.getvalue())

    def test_pending_developer_release_counts_as_approved(self):
        args = self._args()
        with mock.patch.object(cli.flows, "wait_for_review", return_value=self._version("PENDING_DEVELOPER_RELEASE")):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_wait(mock.Mock(), args)
        state = self.journal_mod.list_runs(self.runs_dir)[0]
        self.assertEqual(state["status"], "success")
        self.assertIn("manual release", out.getvalue())

    def test_rejection_fails_the_run_and_exits(self):
        args = self._args()
        with mock.patch.object(cli.flows, "wait_for_review", return_value=self._version("REJECTED")):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_wait(mock.Mock(), args)
        state = self.journal_mod.list_runs(self.runs_dir)[0]
        self.assertEqual(state["status"], "failed")
        self.assertIn("rejection notes", str(ctx.exception))

    def test_webhook_is_called_on_success(self):
        args = self._args(webhook="https://hooks.example/services/X")
        with mock.patch.object(cli.flows, "wait_for_review", return_value=self._version("READY_FOR_SALE")):
            with mock.patch.object(cli, "notify_webhook", return_value=True) as notify:
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    cli.cmd_wait(mock.Mock(), args)
        notify.assert_called_once()
        self.assertIn("READY_FOR_SALE", notify.call_args[0][1])

    def test_webhook_is_called_on_failure(self):
        args = self._args(webhook="https://hooks.example/services/X")
        with mock.patch.object(cli.flows, "wait_for_review", return_value=self._version("REJECTED")):
            with mock.patch.object(cli, "notify_webhook", return_value=True) as notify:
                with self.assertRaises(SystemExit):
                    cli.cmd_wait(mock.Mock(), args)
        notify.assert_called_once()


class ValidateSpecNewFieldsTests(unittest.TestCase):
    def test_clean_new_keys_produce_no_findings(self):
        errors, warnings = cli.validate_spec(
            {
                "version": "1.0.0",
                "releaseDate": "2027-01-15T09:00:00+09:00",
                "phasedRelease": True,
                "promotionalText": {"ja": "新機能を公開しました。"},
                "supportUrls": {"ja": "https://kilde.app/support"},
                "marketingUrls": {"ja": "https://kilde.app/"},
                "appNames": {"ja": "Kilde"},
                "privacyUrls": {"ja": "https://kilde.app/privacy"},
                "copyright": "© 2026 Kilde",
            }
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_release_date_without_offset_is_an_error(self):
        errors, _ = cli.validate_spec({"version": "1.0.0", "releaseDate": "2027-01-15T09:00:00"})
        self.assertTrue(any("UTC offset" in e for e in errors))

    def test_bad_urls_are_errors(self):
        errors, _ = cli.validate_spec(
            {
                "version": "1.0.0",
                "supportUrls": {"ja": "kilde.app"},
                "privacyUrls": {"ja": "mailto:x@y.z"},
            }
        )
        self.assertEqual(len([e for e in errors if "http(s) URL" in e]), 2)

    def test_new_text_limits_are_errors(self):
        errors, _ = cli.validate_spec(
            {"version": "1.0.0", "promotionalText": {"ja": "字" * 171}, "appNames": {"ja": "字" * 31}}
        )
        self.assertTrue(any("170-character" in e for e in errors))
        self.assertTrue(any("30-character" in e for e in errors))

    def test_phased_release_with_manual_release_type_warns(self):
        _, warnings = cli.validate_spec(
            {"version": "1.0.0", "phasedRelease": True, "releaseType": "MANUAL"}
        )
        self.assertTrue(any("phased release" in w for w in warnings))

    def test_placeholder_in_promotional_text_is_an_error(self):
        errors, _ = cli.validate_spec({"version": "1.0.0", "promotionalText": {"ja": "TBD"}})
        self.assertTrue(any("placeholder" in e for e in errors))


class SpecPlanNewFieldsTests(unittest.TestCase):
    def test_plan_lists_release_controls_and_new_fields(self):
        plan = cli.spec_plan(
            {
                "version": "1.0.0",
                "releaseDate": "2027-01-15T09:00:00+09:00",
                "phasedRelease": True,
                "build": "8",
                "whatsNew": {"ja": "x"},
                "promotionalText": {"ja": "y"},
                "supportUrls": {"ja": "https://kilde.app/support"},
                "appNames": {"ja": "Kilde"},
                "privacyUrls": {"ja": "https://kilde.app/privacy"},
                "copyright": "© 2026 Kilde",
                "submit": True,
            }
        )
        self.assertIn("schedule release for 2027-01-15T09:00:00+09:00", plan)
        self.assertIn("phased release on", plan)
        self.assertIn("set promotional text for: ja", plan)
        self.assertIn("set support URLs for: ja", plan)
        self.assertIn("set app names for: ja (app-level)", plan)
        self.assertIn("set privacy URLs for: ja (app-level)", plan)
        self.assertIn("set copyright: © 2026 Kilde", plan)


class RunSpecNewFieldsTests(unittest.TestCase):
    """run_spec wires the release controls and new metadata into their flows."""

    def test_wiring(self):
        client = mock.Mock()
        client.get_all.return_value = []
        client.post.return_value = {"data": {"id": "V1", "attributes": {}}}
        spec = {
            "version": "1.0.0",
            "releaseDate": "2027-01-15T09:00:00+09:00",
            "phasedRelease": True,
            "promotionalText": {"ja": "x"},
            "supportUrls": {"ja": "https://kilde.app/support"},
            "appNames": {"ja": "Kilde"},
            "privacyUrls": {"ja": "https://kilde.app/privacy"},
            "copyright": "© 2026 Kilde",
        }
        with (
            mock.patch.object(cli.flows, "set_release_date") as set_date,
            mock.patch.object(cli.flows, "enable_phased_release") as enable,
            mock.patch.object(cli.flows, "set_localizations") as set_locs,
            mock.patch.object(cli.flows, "set_app_info_fields") as set_app,
            mock.patch.object(cli.flows, "set_copyright") as set_copy,
        ):
            cli.run_spec(client, "APP", spec, False, True, cli.NullJournal())
        set_date.assert_called_once()
        self.assertEqual(set_date.call_args[0][2], "2027-01-15T09:00:00+09:00")
        enable.assert_called_once()
        set_copy.assert_called_once_with(client, "V1", "© 2026 Kilde")
        self.assertEqual(set_locs.call_args[1]["promotional_text"], {"ja": "x"})
        self.assertEqual(set_locs.call_args[1]["support_urls"], {"ja": "https://kilde.app/support"})
        self.assertEqual(set_app.call_args[1]["names"], {"ja": "Kilde"})
        self.assertEqual(set_app.call_args[1]["privacy_urls"], {"ja": "https://kilde.app/privacy"})

    def test_phased_release_false_disables(self):
        client = mock.Mock()
        client.get_all.return_value = []
        client.post.return_value = {"data": {"id": "V1", "attributes": {}}}
        with (
            mock.patch.object(cli.flows, "disable_phased_release") as disable,
            mock.patch.object(cli.flows, "enable_phased_release") as enable,
        ):
            cli.run_spec(client, "APP", {"version": "1.0.0", "phasedRelease": False}, False, True, cli.NullJournal())
        disable.assert_called_once()
        enable.assert_not_called()


class ReleaseControlCommandTests(unittest.TestCase):
    def _version_client(self):
        client = mock.Mock()
        client.get_all.return_value = [
            {"id": "V1", "attributes": {"versionString": "0.9.0", "appStoreState": "WAITING_FOR_REVIEW"}}
        ]
        return client

    def test_phased_release_status_when_disabled(self):
        with mock.patch.object(cli.flows, "get_phased_release", return_value=None):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_phased_release(self._version_client(), mock.Mock(app="A", version="0.9.0", on=False, off=False))
        self.assertIn("disabled", out.getvalue())

    def test_phased_release_status_when_active(self):
        phased = {"attributes": {"state": "ACTIVE", "currentDay": 3, "totalDays": 7}}
        with mock.patch.object(cli.flows, "get_phased_release", return_value=phased):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_phased_release(self._version_client(), mock.Mock(app="A", version="0.9.0", on=False, off=False))
        self.assertIn("day 3/7", out.getvalue())

    def test_phased_release_on_and_off(self):
        for flag, func in (("on", "enable_phased_release"), ("off", "disable_phased_release")):
            with mock.patch.object(cli.flows, func) as target:
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    args = mock.Mock(app="A", version="0.9.0", on=flag == "on", off=flag == "off")
                    cli.cmd_phased_release(self._version_client(), args)
            target.assert_called_once()

    def test_schedule_release_patches_and_prints(self):
        args = mock.Mock(app="A", version="0.9.0", at="2027-01-15T09:00:00+09:00")
        with mock.patch.object(cli.flows, "set_release_date") as set_date:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                cli.cmd_schedule_release(self._version_client(), args)
        set_date.assert_called_once_with(mock.ANY, "V1", "2027-01-15T09:00:00+09:00")
        self.assertIn("scheduled", out.getvalue())


class NewMetadataCommandTests(unittest.TestCase):
    def _version_client(self):
        client = mock.Mock()
        client.get_all.return_value = [
            {"id": "V1", "attributes": {"versionString": "0.9.0", "appStoreState": "PREPARE_FOR_SUBMISSION"}}
        ]
        return client

    def test_promotional_text_command_sets_the_field(self):
        args = mock.Mock(app="A", version="0.9.0", locale="ja", text="新機能", file=None)
        with mock.patch.object(cli.flows, "set_localizations") as set_locs:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                cli.cmd_promotional_text(self._version_client(), args)
        set_locs.assert_called_once_with(mock.ANY, "V1", promotional_text={"ja": "新機能"})

    def test_app_name_command_sets_the_field(self):
        args = mock.Mock(app="A", version="0.9.0", locale="ja", text="Kilde", file=None)
        with mock.patch.object(cli.flows, "set_app_info_fields") as set_app:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                cli.cmd_app_name(mock.Mock(), args)
        set_app.assert_called_once_with(mock.ANY, "A", names={"ja": "Kilde"})


class NotifyWebhookTests(unittest.TestCase):
    @mock.patch("asc_submit.client._curl")
    def test_posts_slack_style_json(self, curl):
        curl.return_value = (200, b"ok")
        self.assertTrue(cli.notify_webhook("https://example/hook", "hello", log=lambda m: None))
        method, url, headers, data, timeout = curl.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://example/hook")
        self.assertEqual(json.loads(data), {"text": "hello"})

    @mock.patch("asc_submit.client._curl")
    def test_http_error_is_swallowed(self, curl):
        curl.return_value = (500, b"boom")
        self.assertFalse(cli.notify_webhook("https://example/hook", "hello", log=lambda m: None))

    @mock.patch("asc_submit.client._curl", side_effect=SystemExit("curl missing"))
    def test_curl_missing_does_not_raise(self, curl):
        self.assertFalse(cli.notify_webhook("https://example/hook", "hello", log=lambda m: None))
