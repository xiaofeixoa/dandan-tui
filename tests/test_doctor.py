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
