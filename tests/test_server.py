import json
import tempfile
import threading
import urllib.error
import urllib.request
import unittest

from asc_submit import journal as j
from asc_submit import server


def get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.read().decode("utf-8")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs_dir = tempfile.mkdtemp()
        jr = j.RunJournal(command="run", title="app 42 → 1.2.0", meta={"app": "42"}, runs_dir=cls.runs_dir)
        jr.start()
        with jr.step("create/resolve version 1.2.0"):
            jr.log("version 1.2.0 ready (V1)")
        with jr.step("update localizations (ja)"):
            jr.log("localizations updated")
        jr.succeed()
        cls.run_id = jr.run_id

        # a second, still-running run to verify live state reads
        cls.live = j.RunJournal(command="upload", title="upload MyApp 0.9.0 (3)", runs_dir=cls.runs_dir)
        cls.live.start()
        with cls.live.step("archive MyApp"):
            cls.live.log("xcodebuild archive started")

        cls.httpd = server.make_server(runs_dir=cls.runs_dir, host="127.0.0.1", port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_runs_list_is_json_newest_first(self):
        status, body = get(f"{self.base}/api/runs")
        self.assertEqual(status, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual([r["id"] for r in runs], [self.live.run_id, self.run_id])

    def test_run_detail_includes_steps(self):
        _, body = get(f"{self.base}/api/runs/{self.run_id}")
        state = json.loads(body)
        self.assertEqual(state["title"], "app 42 → 1.2.0")
        self.assertEqual([s["name"] for s in state["steps"]],
                         ["create/resolve version 1.2.0", "update localizations (ja)"])
        self.assertEqual(state["meta"], {"app": "42"})
        self.assertGreaterEqual(state["duration_seconds"], 0)

    def test_live_run_reflects_updates_without_restart(self):
        _, body = get(f"{self.base}/api/runs/{self.live.run_id}")
        self.assertEqual(json.loads(body)["status"], "running")
        self.live.succeed()
        _, body = get(f"{self.base}/api/runs/{self.live.run_id}")
        self.assertEqual(json.loads(body)["status"], "success")

    def test_log_slice_serves_step_ranges(self):
        _, body = get(f"{self.base}/api/runs/{self.run_id}")
        steps = json.loads(body)["steps"]
        first = steps[0]
        _, body = get(f"{self.base}/api/runs/{self.run_id}/log?from={first['log_start']}&to={first['log_end']}")
        payload = json.loads(body)
        self.assertEqual(payload["text"], "version 1.2.0 ready (V1)\n")
        self.assertEqual(payload["next"], first["log_end"])

    def test_log_open_ended_slice_returns_size(self):
        _, body = get(f"{self.base}/api/runs/{self.run_id}/log?from=0")
        payload = json.loads(body)
        self.assertEqual(payload["size"], len("version 1.2.0 ready (V1)\nlocalizations updated\n"))
        self.assertEqual(payload["next"], payload["size"])

    def test_log_with_from_only(self):
        _, body = get(f"{self.base}/api/runs/{self.run_id}")
        boundary = json.loads(body)["steps"][0]["log_end"]
        _, body = get(f"{self.base}/api/runs/{self.run_id}/log?from={boundary}")
        payload = json.loads(body)
        self.assertEqual(payload["text"], "localizations updated\n")

    def test_dashboard_html_is_served(self):
        for path in ("/", f"/runs/{self.run_id}"):
            status, body = get(f"{self.base}{path}")
            self.assertEqual(status, 200)
            self.assertIn("asc-submit workflows", body)
            self.assertIn("/api/runs", body)

    def test_unknown_run_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get(f"{self.base}/api/runs/20260928-123456-abcd")
        self.assertEqual(ctx.exception.code, 404)

    def test_traversal_ids_are_rejected(self):
        for bad in ("..%2F..%2Fetc", "deadbeef"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                get(f"{self.base}/api/runs/{bad}")
            self.assertEqual(ctx.exception.code, 404)

    def test_unknown_api_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get(f"{self.base}/api/nonsense")
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_log_params_are_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get(f"{self.base}/api/runs/{self.run_id}/log?from=abc")
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
