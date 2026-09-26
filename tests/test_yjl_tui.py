"""yjl_tui.tui 的分发注册表、辅助助手与入口参数测试（不依赖 Linux 环境）。"""
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tui
from yjl_tui import paths


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tui = tui.TUI({"categories": [{"id": "c", "title": "C"}], "actions": []})
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "demo.log"

    def test_exit_kind_sets_should_exit_without_pause(self):
        with mock.patch("yjl_tui.tui.pause", side_effect=AssertionError("exit 不应暂停")):
            rc = self.tui.dispatch({"id": "exit", "kind": "exit", "title": "q"}, self.log)
        self.assertEqual(rc, 0)
        self.assertTrue(self.tui.should_exit)

    def test_unknown_builtin_returns_1_with_message(self):
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            rc = self.tui.dispatch({"id": "no_such_builtin", "title": "?"}, self.log)
        self.assertEqual(rc, 1)
        self.assertIn("no_such_builtin", out.getvalue())

    def test_builtin_fallback_runs_interactive(self):
        with mock.patch.object(self.tui, "interactive", return_value=7) as interactive:
            rc = self.tui.dispatch({"id": "swap_check", "title": "swap"}, self.log)
        self.assertEqual(rc, 7)
        interactive.assert_called_once()
        self.assertEqual(interactive.call_args.args[0], tui.TUI.builtin("swap_check"))

    def test_handler_registry_routes_and_pauses_once(self):
        with mock.patch.object(self.tui, "tcp_status", return_value=0) as status, \
             mock.patch("yjl_tui.tui.pause") as pause:
            rc = self.tui.dispatch({"id": "tcp_status", "title": "TCP 状态"}, self.log)
        status.assert_called_once_with(self.log)
        self.assertEqual(rc, 0)
        pause.assert_called_once()

    def test_tcp_profile_routes_to_apply(self):
        with mock.patch.object(self.tui, "tcp_apply_profile", return_value=0) as apply_profile, \
             mock.patch("yjl_tui.tui.pause"):
            rc = self.tui.dispatch({"id": "tcp_bbr_fq", "tcp_profile": "bbr-fq", "title": "BBR"}, self.log)
        apply_profile.assert_called_once_with("bbr-fq", self.log)
        self.assertEqual(rc, 0)

    def test_local_script_chains_repair_for_tcp_brutal_install(self):
        with mock.patch.object(self.tui, "local_script", return_value=0) as local_script, \
             mock.patch("yjl_tui.tcp_brutal.repair", return_value=5) as repair:
            rc = self.tui.dispatch(
                {"id": "tcp_brutal_install", "kind": "local_script", "path": "scripts/install-tcp-brutal.sh", "title": "t"},
                self.log,
            )
        local_script.assert_called_once()
        repair.assert_called_once_with(self.log)
        self.assertEqual(rc, 5)
        self.assertEqual(rc, 5)

    def test_local_script_success_without_install_id_skips_repair(self):
        with mock.patch.object(self.tui, "local_script", return_value=0) as local_script, \
             mock.patch("yjl_tui.tcp_brutal.repair") as repair:
            rc = self.tui.dispatch(
                {"id": "tcpfit", "kind": "local_script", "path": "scripts/tcpfit/tcpfit.sh", "title": "t"},
                self.log,
            )
        repair.assert_not_called()
        self.assertEqual(rc, 0)

    def test_execute_root_gate_reports_and_pauses(self):
        with mock.patch("yjl_tui.tui.IS_ROOT", False), \
             mock.patch("yjl_tui.tui.LOGS", Path(self._tmp.name)), \
             mock.patch("sys.stdout", new_callable=StringIO), \
             mock.patch("builtins.input") as user_input:
            self.tui.execute({"id": "tcp_status", "title": "TCP 状态", "needs_root": True})
        self.assertEqual(self.tui.status, "权限不足，未执行")
        user_input.assert_called_once()

    def test_execute_cancelled_by_confirm(self):
        with mock.patch("yjl_tui.tui.LOGS", Path(self._tmp.name)), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.tui.execute({"id": "apt_upgrade", "title": "升级", "needs_root": True})
        self.assertEqual(self.tui.status, "已取消")


class HelperTests(unittest.TestCase):
    def test_ask_returns_default_on_empty_input(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(tui.ask("选择"), "1")
        with mock.patch("builtins.input", return_value=" 3 "):
            self.assertEqual(tui.ask("选择"), "3")

    def test_restore_backup_restores_or_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conf"
            target.write_text("new", encoding="utf-8")
            backup = Path(tmp) / "conf.bak"
            backup.write_text("old", encoding="utf-8")
            tui.TUI.restore_backup(target, backup)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            tui.TUI.restore_backup(target, None)
            self.assertFalse(target.exists())

    def test_run_captured_timeout_returns_124(self):
        with mock.patch("yjl_tui.tui.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=1)), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(tui.TUI._run_captured(["x"], None, 1, "Docker "), 124)

    def test_prompt_script_args_parses_quoted_values(self):
        with mock.patch("builtins.input", return_value="-debian 12 -pwd 'my password'"), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(tui.TUI.prompt_script_args("参数"), ["-debian", "12", "-pwd", "my password"])

    def test_prompt_script_args_empty_and_invalid_fall_back_to_no_args(self):
        with mock.patch("builtins.input", return_value=""), mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(tui.TUI.prompt_script_args("参数"), [])
        with mock.patch("builtins.input", return_value="-debian '12"), mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(tui.TUI.prompt_script_args("参数"), [])

    def test_online_prompt_args_are_appended_after_static_args(self):
        manager = tui.TUI({"categories": [], "actions": []})
        action = {
            "id": "ns_dd_leitbogioro", "url": "https://example.invalid/install.sh",
            "args": [], "prompt_args": "输入 InstallNET 参数",
        }
        with mock.patch.object(manager, "download", return_value=Path("/tmp/install.sh")), \
             mock.patch.object(manager, "syntax_ok", return_value=True), \
             mock.patch("builtins.input", return_value="-debian 12 -pwd 'pw'"), \
             mock.patch.object(manager, "interactive", return_value=0) as interactive, \
             mock.patch("sys.stdout", new_callable=StringIO):
            rc = manager.online(action, Path('/tmp/demo.log'))
        self.assertEqual(rc, 0)
        argv = interactive.call_args.args[0]
        self.assertEqual(argv, ["bash", str(Path("/tmp/install.sh")), "-debian", "12", "-pwd", "pw"])

    def test_online_without_prompt_keeps_static_args_only(self):
        manager = tui.TUI({"categories": [], "actions": []})
        action = {"id": "ns_bench", "url": "https://example.invalid/bench.sh", "args": ["--fast"]}
        with mock.patch.object(manager, "download", return_value=Path("/tmp/bench.sh")), \
             mock.patch.object(manager, "syntax_ok", return_value=True), \
             mock.patch("builtins.input", side_effect=AssertionError("无 prompt_args 不应提示")), \
             mock.patch.object(manager, "interactive", return_value=0) as interactive, \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(manager.online(action, Path('/tmp/demo.log')), 0)
        self.assertEqual(interactive.call_args.args[0], ["bash", str(Path("/tmp/bench.sh")), "--fast"])

    def test_docker_run_returns_127_without_docker(self):
        manager = tui.TUI({"categories": [], "actions": []})
        with mock.patch("yjl_tui.tui.shutil.which", return_value=None), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(manager.docker_run(["ps"]), 127)


class EntryTests(unittest.TestCase):
    def test_version_flag(self):
        with mock.patch("sys.argv", ["tui.py", "--version"]), mock.patch("sys.stdout", new_callable=StringIO) as out:
            self.assertEqual(tui.main(), 0)
        self.assertIn(paths.VERSION, out.getvalue())

    def test_doctor_flag_propagates_exit_code(self):
        with mock.patch("sys.argv", ["tui.py", "--doctor"]), \
             mock.patch("yjl_tui.tui.run_doctor", return_value=0) as run_doctor:
            self.assertEqual(tui.main(), 0)
        run_doctor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
