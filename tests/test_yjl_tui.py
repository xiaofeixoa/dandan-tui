"""yjl_tui.tui 的分发注册表、辅助助手与入口参数测试（不依赖 Linux 环境）。"""
import json
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
        local_script.assert_called_once()
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

    def test_build_dd_args_leitbogioro_linux_and_windows(self):
        args = tui.TUI.build_dd_args("leitbogioro", "debian", "12", "Pwd@123", "")
        self.assertEqual(args, ["-debian", "12", "-pwd", "Pwd@123"])
        args = tui.TUI.build_dd_args("leitbogioro", "debian", "12", "Pwd@123", "2222", firmware=True)
        self.assertEqual(args, ["-debian", "12", "-pwd", "Pwd@123", "-port", "2222", "-firmware"])
        args = tui.TUI.build_dd_args("leitbogioro", "windows", "10", "", "", lang="cn")
        self.assertEqual(args, ["-windows", "10", "-lang", "cn"])

    def test_build_dd_args_moeclub_and_validation(self):
        args = tui.TUI.build_dd_args("moeclub", "d", "11", "Pwd@123", "2222", firmware=True)
        self.assertEqual(args, ["-d", "11", "-v", "64", "-p", "Pwd@123", "-a", "-port", "2222", "-firmware"])
        with self.assertRaises(ValueError):
            tui.TUI.build_dd_args("moeclub", "windows", "10", "x", "")
        for bad in (("", "12", "pw", ""), ("debian", "", "pw", ""), ("debian", "12", "", ""), ("debian", "12", "pw", "99999")):
            with self.assertRaises(ValueError):
                tui.TUI.build_dd_args("leitbogioro", bad[0], bad[1], bad[2], bad[3])

    def test_dd_reinstall_wizard_full_flow(self):
        manager = tui.TUI({"categories": [], "actions": []})
        script = Path("/tmp/InstallNET.sh")
        answers = iter(["yes", "1", "", "", "", "DD"])  # 确认 / Debian / 版本默认 / 端口默认 / firmware 默认 / 最终确认
        with mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
             mock.patch("yjl_tui.tui.getpass.getpass", return_value="Pwd@123"), \
             mock.patch.object(manager, "interactive", return_value=0) as interactive, \
             mock.patch("sys.stdout", new_callable=StringIO) as out:
            rc = manager.dd_reinstall({"dd_variant": "leitbogioro"}, Path("/tmp/dd.log"), script)
        self.assertEqual(rc, 0)
        argv = interactive.call_args.args[0]
        self.assertEqual(argv, ["bash", str(script), "-debian", "12", "-pwd", "Pwd@123"])
        self.assertFalse(interactive.call_args.kwargs.get("pause_after", True))
        # 执行阶段的命令展示不带密码；执行前的汇总预览里密码已打码。
        self.assertNotIn("Pwd@123", interactive.call_args.kwargs["display_cmd"])
        self.assertIn("******", out.getvalue())
        self.assertNotIn("-pwd Pwd@123", out.getvalue())

    def test_dd_reinstall_cancelled_before_anything(self):
        manager = tui.TUI({"categories": [], "actions": []})
        with mock.patch("builtins.input", return_value="no"), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(manager.dd_reinstall({"dd_variant": "leitbogioro"}, Path("/tmp/dd.log"), Path("/tmp/i.sh")), 2)

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


class VirtualCategoryTests(unittest.TestCase):
    ACTIONS = [
        {"id": "act_a", "category": "c1", "title": "动作A", "description": "描述A", "kind": "builtin"},
        {"id": "act_b", "category": "c2", "title": "动作B", "description": "描述B", "kind": "builtin"},
        {"id": "act_c", "category": "c2", "title": "Nginx 特殊动作", "description": "描述C", "kind": "builtin"},
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        state_patcher = mock.patch("yjl_tui.tui.STATE", self.state)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)
        self.manager = tui.TUI({
            "categories": [{"id": "c1", "title": "分类1"}, {"id": "c2", "title": "分类2"}],
            "actions": self.ACTIONS,
        })

    def test_filter_actions_matches_id_title_description(self):
        actions = tui.filter_actions(self.ACTIONS, "nginx")
        self.assertEqual([a["id"] for a in actions], ["act_c"])
        self.assertEqual([a["id"] for a in tui.filter_actions(self.ACTIONS, "ACT_A")], ["act_a"])
        self.assertEqual(tui.filter_actions(self.ACTIONS, "  "), [])
        self.assertEqual(tui.filter_actions(self.ACTIONS, "不存在"), [])

    def test_recent_virtual_category_orders_by_last_time(self):
        (self.state / "usage.json").write_text(
            json.dumps({
                "act_a": {"count": 2, "last": "2026-09-27T09:00:00"},
                "act_b": {"count": 1, "last": "2026-09-27T10:00:00"},
                "act_gone": {"count": 9, "last": "2026-09-27T11:00:00"},
            }),
            encoding="utf-8",
        )
        self.manager.rebuild_categories()
        self.assertEqual(self.manager.categories[0]["id"], tui.TUI.RECENT_CATEGORY_ID)
        # rebuild 保留当前分类选中；显式切到「最近使用」后应按最近时间倒序展示
        self.manager.category = 0
        self.assertEqual([a["id"] for a in self.manager.current_actions()], ["act_b", "act_a"])

    def test_execute_records_usage_and_rebuild(self):
        with mock.patch("yjl_tui.tui.STATE", self.state),              mock.patch("yjl_tui.tui.LOGS", self.state),              mock.patch.object(self.manager, "dispatch", return_value=0),              mock.patch("sys.stdout", new_callable=StringIO):
            self.manager.execute({"id": "act_a", "title": "动作A"})
        usage = json.loads((self.state / "usage.json").read_text(encoding="utf-8"))
        self.assertIn("act_a", usage)
        # 无使用记录时不出现「最近使用」；记录后 rebuild 出现在分类最前。
        with mock.patch("yjl_tui.tui.STATE", self.state):
            self.manager.rebuild_categories()
        self.assertEqual(self.manager.categories[0]["id"], tui.TUI.RECENT_CATEGORY_ID)

    def test_search_virtual_category_and_clear(self):
        self.manager.set_search("nginx")
        self.assertEqual(self.manager.search_query, "nginx")
        self.assertEqual([a["id"] for a in self.manager.current_actions()], ["act_c"])
        self.assertEqual(self.manager.categories[self.manager.category]["id"], tui.TUI.SEARCH_CATEGORY_ID)
        self.manager.set_search("")
        self.assertEqual(self.manager.search_matches, [])
        self.assertNotIn(tui.TUI.SEARCH_CATEGORY_ID, [c["id"] for c in self.manager.categories])

    def test_usage_file_corruption_falls_back_to_empty(self):
        (self.state / "usage.json").write_text("{ broken", encoding="utf-8")
        with mock.patch("yjl_tui.tui.STATE", self.state):
            self.assertEqual(self.manager.recent_ids(), [])
            self.manager.record_usage("act_a")  # 损坏文件被重置而不是永久失效
            self.assertEqual(self.manager.recent_ids(), ["act_a"])


class FakeScreen:
    """run_ui/draw 所需的最小 screen 接口；按键由脚本回放。"""

    def __init__(self, keys):
        self._keys = list(keys)

    def getmaxyx(self):
        return 24, 100

    def erase(self):
        pass

    def addnstr(self, *args):
        pass

    def vline(self, *args):
        pass

    def hline(self, *args):
        pass

    def refresh(self):
        pass

    def clear(self):
        pass

    def keypad(self, *args):
        pass

    def getch(self):
        return self._keys.pop(0) if self._keys else ord("q")


@unittest.skipUnless(tui.curses is not None, "需要 curses 模块（windows 装 windows-curses）")
class UITests(unittest.TestCase):
    """用 FakeScreen 驱动 run_ui：导航、Tab 切分类、/ 搜索、Enter 执行、最近使用联动。"""

    ACTIONS = [
        {"id": "act_swap", "category": "c1", "title": "Swap 工具", "description": "d", "kind": "builtin"},
        {"id": "act_docker", "category": "c1", "title": "Docker 工具", "description": "d", "kind": "builtin"},
        {"id": "act_bench", "category": "c2", "title": "Bench 测试", "description": "d", "kind": "builtin"},
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        for target, value, create in (
            ("yjl_tui.tui.STATE", self.state, False),
            ("yjl_tui.tui.LOGS", self.state, False),
            ("yjl_tui.tui.curses.curs_set", mock.Mock(), False),
            ("yjl_tui.tui.curses.use_default_colors", mock.Mock(), False),
            ("yjl_tui.tui.curses.init_pair", mock.Mock(), False),
            ("yjl_tui.tui.curses.endwin", mock.Mock(), False),
            ("yjl_tui.tui.curses.color_pair", mock.Mock(return_value=0), False),
            ("yjl_tui.tui.curses.ACS_VLINE", 0, True),
            ("yjl_tui.tui.curses.ACS_HLINE", 0, True),
        ):
            patcher = mock.patch(target, value, create=create)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = tui.TUI({
            "categories": [{"id": "c1", "title": "分类1"}, {"id": "c2", "title": "分类2"}],
            "actions": self.ACTIONS,
        })

    def run_keys(self, keys, inputs=None):
        screen = FakeScreen(keys)
        input_iter = iter(inputs or [])
        with mock.patch("builtins.input", side_effect=lambda *_: next(input_iter, "")), \
             mock.patch("sys.stdout", new_callable=StringIO):
            self.manager.run_ui(screen)

    def test_navigation_moves_selection_and_quit(self):
        self.run_keys([ord("j"), ord("j"), ord("k"), ord("q")])
        self.assertEqual(self.manager.selected, 1)
        self.assertFalse(self.manager.should_exit)

    def test_tab_switches_category(self):
        self.run_keys([9, ord("q")])
        # 初始无使用记录时分类 = [c1, c2]，Tab 后应到 c2
        self.assertEqual(self.manager.categories[self.manager.category]["id"], "c2")

    def test_search_key_filters_and_enter_executes(self):
        with mock.patch.object(self.manager, "dispatch", return_value=0):
            self.run_keys([ord("/"), 10, ord("q")], inputs=["docker"])
        usage = json.loads((self.state / "usage.json").read_text(encoding="utf-8"))
        self.assertIn("act_docker", usage)
        self.assertEqual(self.manager.categories[self.manager.category]["id"], tui.TUI.SEARCH_CATEGORY_ID)

    def test_enter_executes_and_recent_category_appears(self):
        recorded = []
        with mock.patch.object(self.manager, "dispatch", side_effect=lambda a, log: recorded.append(a["id"]) or 0):
            self.run_keys([10, ord("q")])
        self.assertEqual(recorded, ["act_swap"])
        self.assertTrue((self.state / "usage.json").is_file())
        self.assertEqual(self.manager.categories[0]["id"], tui.TUI.RECENT_CATEGORY_ID)

    def test_search_without_match_keeps_menu_usable(self):
        self.run_keys([ord("/"), ord("q")], inputs=["不存在的关键字"])
        self.assertEqual(self.manager.search_matches, [])
        self.assertTrue(self.manager.search_query)


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
