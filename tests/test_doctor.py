"""yjl_tui.doctor 自诊断检查。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yjl_tui import doctor


def make_app(app_dir: Path, actions: list | None = None) -> None:
    if actions is None:
        actions = [{"id": "demo", "category": "c", "title": "1. demo", "description": "d", "kind": "builtin"}]
    (app_dir / "scripts.json").write_text(
        json.dumps(
            {"categories": [{"id": "c", "title": "C"}], "actions": actions},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (app_dir / "tcp_profiles.json").write_text(json.dumps({"bbr-fq": {"title": "x", "settings": {}}}), encoding="utf-8")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime = Path(self._tmp.name)
        self.cache = self.runtime / "cache"
        self.logs = self.runtime / "logs"
        self.state = self.runtime / "state"

    def levels(self, results):
        return {level for level, _ in results}

    def test_repo_checkout_has_no_failures(self):
        results = doctor.collect_checks(app_dir=ROOT, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
        self.assertNotIn("fail", self.levels(results), results)

    def test_missing_local_script_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            make_app(app, actions=[
                {"id": "broken", "category": "c", "title": "1. broken", "description": "d",
                 "kind": "local_script", "path": "scripts/does-not-exist.sh", "interpreter": "bash", "args": []},
            ])
            results = doctor.collect_checks(app_dir=app, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
        self.assertIn("fail", self.levels(results))
        messages = " ".join(message for _, message in results)
        self.assertIn("does-not-exist.sh", messages)

    def test_invalid_scripts_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            (app / "scripts.json").write_text("{ not json", encoding="utf-8")
            results = doctor.collect_checks(app_dir=app, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
        self.assertIn("fail", self.levels(results))

    def test_missing_tcp_profiles_is_warning_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            make_app(app)
            (app / "tcp_profiles.json").unlink()
            results = doctor.collect_checks(app_dir=app, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
        self.assertNotIn("fail", self.levels(results))
        self.assertIn("warn", self.levels(results))

    def test_launch_manifest_checks_detect_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            make_app(app)
            (app / "tui.py").write_text("print('tui')\n", encoding="utf-8")
            (app / "launch.sh").write_text('FILES=(\n    "tui.py|600"\n)\n', encoding="utf-8")

            import hashlib

            digest = hashlib.sha256((app / "tui.py").read_bytes()).hexdigest()
            (app / "SHA256SUMS").write_text(f"{digest}  tui.py\n", encoding="utf-8")
            results = doctor.collect_checks(app_dir=app, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
            self.assertNotIn("fail", self.levels(results))

            (app / "tui.py").write_text("print('changed')\n", encoding="utf-8")
            results = doctor.collect_checks(app_dir=app, cache_dir=self.cache, logs_dir=self.logs, state_dir=self.state)
            self.assertIn("fail", self.levels(results))

    def test_collect_network_targets_dedupes_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            make_app(app, actions=[
                {"id": "a", "category": "c", "title": "t", "description": "d",
                 "kind": "online", "url": "https://z.example/x.sh", "interpreter": "bash", "args": []},
                {"id": "b", "category": "c", "title": "t", "description": "d",
                 "kind": "online", "url": "https://a.example/y.sh", "interpreter": "bash", "args": []},
                {"id": "c", "category": "c", "title": "t", "description": "d",
                 "kind": "tcp_online", "url": "https://a.example/y.sh", "interpreter": "bash", "args": []},
                {"id": "d", "category": "c", "title": "t", "description": "d", "kind": "builtin"},
            ])
            targets = doctor.collect_network_targets(app_dir=app)
        self.assertEqual(targets, ["https://a.example/y.sh", "https://z.example/x.sh"])

    def test_probe_urls_reports_ok_and_dead(self):
        import http.server
        import threading

        class _OKHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OKHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            live = f"http://127.0.0.1:{server.server_address[1]}/live"
            dead = "http://127.0.0.1:1/dead"
            results = dict(doctor.probe_urls([live, dead], timeout=3, workers=2))
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(results[live].startswith("HTTP 200"))
        self.assertTrue(results[dead].startswith("ERR"))

    def test_run_doctor_exit_code(self):
        class Buffer:
            @staticmethod
            def write(text):
                pass

        with mock.patch.object(doctor, "collect_checks", return_value=[("ok", "fine"), ("warn", "meh")]):
            self.assertEqual(doctor.run_doctor(Buffer()), 0)
        with mock.patch.object(doctor, "collect_checks", return_value=[("fail", "broken")]):
            self.assertEqual(doctor.run_doctor(Buffer()), 1)


if __name__ == "__main__":
    unittest.main()
