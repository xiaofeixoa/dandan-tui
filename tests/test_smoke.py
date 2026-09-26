import hashlib
import json
import os
import re
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

# TUI 实现拆分在 yjl_tui 包内；针对类方法行为的源码字符串断言统一读这里。
TUI_SOURCE = (ROOT / "yjl_tui" / "tui.py").read_text(encoding="utf-8")


def launch_file_list() -> list:
    """Parse the launch.sh FILES manifest into (path, mode) pairs."""
    launcher = (ROOT / "launch.sh").read_text(encoding="utf-8")
    return re.findall(r'(?m)^\s+"([^"]+)\|([0-9]{3})",?$', launcher)


class ConfigSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "scripts.json").read_text(encoding="utf-8"))

    def test_config_shape_and_unique_action_ids(self):
        self.assertIsInstance(self.config["categories"], list)
        self.assertIsInstance(self.config["actions"], list)
        action_ids = [action["id"] for action in self.config["actions"]]
        self.assertEqual(len(action_ids), len(set(action_ids)))
        self.assertGreaterEqual(len(action_ids), 82)

    def test_kernel_categories_and_action_migration(self):
        categories = [category["id"] for category in self.config["categories"]]
        actions = {action["id"]: action for action in self.config["actions"]}
        tcp_index = categories.index("tcp_tuning")
        self.assertEqual(categories[tcp_index + 1:tcp_index + 3], ["kernel_manage", "vpn_kernel"])
        self.assertEqual(actions["system_kernel_maintenance"]["category"], "kernel_manage")
        self.assertTrue(actions["system_kernel_maintenance"]["needs_root"])
        self.assertEqual(actions["grub_manage"]["category"], "kernel_manage")
        self.assertEqual(actions["kernel_manage"]["category"], "vpn_kernel")
        self.assertEqual(actions["tcp_fsc_1"]["category"], "vpn_kernel")
        self.assertEqual(actions["tcp_bbr_fq"]["category"], "tcp_tuning")

    def test_grub_maintenance_uses_discovered_entries_and_never_reboots(self):
        source = TUI_SOURCE
        self.assertIn("kernel_manager.parse_grub_menu_entries", source)
        self.assertIn("kernel_manager.resolve_grub_entry", source)
        self.assertIn("grub-editenv", source)
        self.assertIn("grub-reboot", source)
        self.assertNotIn('"reboot"', source)

    def test_system_kernel_upstream_paths_are_owned_and_verified(self):
        source = TUI_SOURCE
        self.assertIn("yjl-tui-kernel-", source)
        self.assertIn("kernel_manager.debian_backports_source", source)
        self.assertIn("kernel_manager.mainline_sha256sums", source)
        self.assertIn("gpgv", source)
        self.assertIn("dpkg", source)
        key = ROOT / "scripts" / "ubuntu-mainline-signing-key.gpg"
        self.assertTrue(key.is_file())
        provenance = (ROOT / "scripts" / "ubuntu-mainline-signing-key.md").read_text(encoding="utf-8")
        self.assertIn("60AA7B6F30434AE68E569963E50C6A0917C622B0", provenance)
        self.assertIn("ubuntu-mainline-signing-key.gpg", source)
        self.assertIn('"disabled" not in facts.secure_boot.lower()', source)

    def test_kernel_source_builder_is_local_hardened_and_launcher_cached(self):
        root = ROOT / "scripts" / "kernel-installer"
        self.assertTrue((root / "kernel_installer.sh").is_file())
        self.assertTrue((root / "src" / "slib.sh").is_file())
        self.assertIn("MIT License", (root / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("commit", (root / "UPSTREAM.md").read_text(encoding="utf-8"))
        source = (root / "kernel_installer.sh").read_text(encoding="utf-8")
        self.assertNotIn("source <(curl", source)
        self.assertNotIn("--no-check-certificate", source)
        self.assertNotIn("self-update", source.lower())
        self.assertNotIn("--mainline", source)
        self.assertNotIn('"3": "--mainline"', TUI_SOURCE)
        files = dict(launch_file_list())
        for relative, mode in (
            ("scripts/kernel-installer/kernel_installer.sh", "700"),
            ("scripts/kernel-installer/src/slib.sh", "600"),
            ("scripts/kernel-installer/LICENSE", "600"),
            ("scripts/kernel-installer/UPSTREAM.md", "600"),
        ):
            self.assertEqual(files.get(relative), mode)

    def test_launch_manifest_covers_every_downloaded_file(self):
        files = [path for path, _ in launch_file_list()]
        sums = {}
        for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, path = line.split(None, 1)
                sums[path.strip().lstrip("*")] = digest
        self.assertEqual(sorted(sums), sorted(files))
        for path, digest in sums.items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest, path)

    def test_launcher_ships_every_local_script_action(self):
        files = {path for path, _ in launch_file_list()}
        missing = [
            action["path"]
            for action in self.config["actions"]
            if action.get("kind") == "local_script" and action["path"] not in files
        ]
        self.assertEqual(missing, [])

    def test_launcher_ships_every_yjl_tui_module(self):
        # 包内任何 .py 漏出 FILES 清单，部署后 import 就会崩（regression: doctor.py 曾被漏掉）。
        files = {path for path, _ in launch_file_list()}
        modules = {f"yjl_tui/{path.name}" for path in (ROOT / "yjl_tui").glob("*.py")}
        self.assertTrue(modules)
        self.assertEqual(modules - files, set())

    def test_launcher_verifies_downloads_with_sha256sums(self):
        launcher = (ROOT / "launch.sh").read_text(encoding="utf-8")
        self.assertIn('MANIFEST="SHA256SUMS"', launcher)
        self.assertIn("sha256sum -c", launcher)
        self.assertIn("verify_downloads", launcher)
        # 缓存穿透参数默认为空，只有显式设置 YJL_TUI_CACHE_BUSTER 才附加。
        self.assertIn('CACHE_BUSTER="${YJL_TUI_CACHE_BUSTER:-}"', launcher)
        self.assertNotIn("$(date +%s)", launcher)

    def test_doctor_and_version_flags_are_wired(self):
        source = TUI_SOURCE
        self.assertIn('"--doctor"', source)
        self.assertIn('"--version"', source)
        self.assertIn("run_doctor", source)

    def test_dangerous_builtins_require_explicit_confirmation(self):
        manager = tui.TUI({"categories": [], "actions": []})
        for action_id in ("apt_upgrade", "swap_builtin", "docker_prune"):
            # confirm 会先打印提示语；测试里静音 stdout。
            with mock.patch("sys.stdout", new_callable=StringIO),                  mock.patch("builtins.input", return_value="yes"):
                self.assertTrue(manager.confirm({"id": action_id}))
            with mock.patch("sys.stdout", new_callable=StringIO),                  mock.patch("builtins.input", return_value=""):
                self.assertFalse(manager.confirm({"id": action_id}))
        # 其余动作保持原行为：不额外确认。
        with mock.patch("builtins.input", side_effect=AssertionError("不应提示")):
            self.assertTrue(manager.confirm({"risk": "danger"}))
            self.assertTrue(manager.confirm({"id": "system_info"}))

    def test_source_builder_never_accepts_an_external_build_directory_or_old_gpgv2(self):
        source = (ROOT / "scripts" / "kernel-installer" / "kernel_installer.sh").read_text(encoding="utf-8")
        self.assertNotIn("INSTALL_DIR=${INSTALL_DIR", source)
        self.assertNotIn("--dir", source)
        self.assertIn("mktemp -d /var/tmp/yjl-kernel-build.", source)
        self.assertIn("command -v gpgv", source)
        self.assertNotIn("gpgv2", source)

    def test_fixed_online_endpoints_and_local_warp_snapshot(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        warp = actions["warp"]
        self.assertEqual(warp["kind"], "local_script")
        self.assertEqual(warp["path"], "scripts/fscarmen-warp.sh")
        self.assertTrue((ROOT / warp["path"]).is_file())
        self.assertEqual(
            actions["onepanel"]["url"],
            "https://resource.fit2cloud.com/1panel/package/v2/quick_start.sh",
        )

    def test_launcher_downloads_nginx_module(self):
        files = dict(launch_file_list())
        self.assertEqual(files.get("nginx_manager.py"), "600")

    def test_each_action_has_dispatch_or_supported_kind(self):
        source = TUI_SOURCE
        special_ids = {
            "system_info", "apt_upgrade", "log_manage", "kernel_manage", "ssl_manage",
            "network_manage", "grub_manage", "ip_preference", "webdav_manage", "ssh_config",
            "tcp_status", "tcp_remove_all", "docker_status", "docker_containers", "docker_images",
            "docker_logs", "docker_start", "docker_stop", "docker_restart", "docker_exec",
            "docker_pull", "docker_remove", "docker_compose", "docker_prune",
            "docker_daemon_restart", "lazydocker", "custom_script", "nginx_manager",
            "dockerhub_mirror",
        }
        supported_kinds = {"online", "local_script", "tcp_online", "exit"}
        missing = []
        for action in self.config["actions"]:
            if (
                action.get("kind") in supported_kinds
                or action.get("tcp_profile")
                or action["id"] in special_ids
            ):
                continue
            if action["id"] not in source:
                missing.append(action["id"])
        self.assertEqual(missing, [])

    def test_nginx_parser_and_validation(self):
        sample = """# configuration file /etc/nginx/conf.d/demo.conf:\nserver {\n    listen 10301 ssl;\n    server_name demo.example.com;\n    root /var/www/demo;\n    ssl_certificate /etc/letsencrypt/live/demo/fullchain.pem;\n    location / { proxy_pass http://127.0.0.1:9000; }\n}\n"""
        sites = tui.nginx_manager.parse_sites(sample)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].server_name, ["demo.example.com"])
        self.assertIn("10301", sites[0].listen)
        self.assertEqual(sites[0].root, "/var/www/demo")
        self.assertEqual(sites[0].proxy_pass, "http://127.0.0.1:9000")
        self.assertTrue(tui.nginx_manager.valid_domain("demo.example.com"))
        self.assertFalse(tui.nginx_manager.valid_domain("not a domain"))
        self.assertTrue(tui.nginx_manager.valid_port("10301"))
        self.assertFalse(tui.nginx_manager.valid_port("70000"))

    def test_static_site_template_and_root_validation(self):
        content = tui.nginx_manager.static_site_config("static.example.com", "10301", "/var/www/static.example.com")
        self.assertIn("listen 10301;", content)
        self.assertIn("server_name static.example.com;", content)
        self.assertIn("root /var/www/static.example.com;", content)
        self.assertIn("try_files $uri $uri/ =404;", content)
        self.assertTrue(tui.nginx_manager.valid_web_root("/var/www/static.example.com"))
        self.assertFalse(tui.nginx_manager.valid_web_root("relative/path"))
        self.assertFalse(tui.nginx_manager.valid_web_root("/var/www;bad"))
        for protected in ("/etc", "/etc/nginx", "/boot", "/boot/grub", "/usr", "/usr/share/nginx", "/var/log", "/var/lib"):
            self.assertFalse(tui.nginx_manager.valid_web_root(protected), protected)
        self.assertTrue(tui.nginx_manager.valid_web_root("/srv/www/site"))

    def test_nginx_ui_is_lazy_user_repo_action(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["nginx_ui"]
        self.assertEqual(action["category"], "server")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "tools/nginx-ui/install.sh")
        self.assertTrue((ROOT / action["path"]).is_file())
        launcher = (ROOT / "launch.sh").read_text(encoding="utf-8")
        # 安装器与校验清单随 launch.sh 缓存；大体积面板归档保持惰性，不进 FILES。
        self.assertIn("tools/nginx-ui/install.sh", launcher)
        self.assertIn("tools/nginx-ui/SHA256SUMS", launcher)
        self.assertNotIn("tools/nginx-ui/nginx-ui-linux", launcher)
        installer = (ROOT / "tools/nginx-ui/install.sh").read_text(encoding="utf-8")
        self.assertIn("RELEASE_PINNED='v2.5.7'", installer)
        self.assertIn("raw.githubusercontent.com/xiaofeixoa/dandan-tui/main/tools/nginx-ui", installer)
        self.assertIn("NGINX_UI_LOCAL_SOURCE", installer)
        self.assertNotIn("github.com/dandan8511", installer)

    def test_nodeseek_collection_category(self):
        categories = {category["id"]: category["title"] for category in self.config["categories"]}
        self.assertEqual(categories.get("nodeseek_collection"), "nodeseek合集")
        actions = [action for action in self.config["actions"] if action.get("category") == "nodeseek_collection"]
        self.assertGreaterEqual(len(actions), 35)
        ids = [action["id"] for action in actions]
        self.assertEqual(len(ids), len(set(ids)))
        for action in actions:
            self.assertEqual(action["kind"], "online", action["id"])
            self.assertTrue(action["url"].startswith("https://"), action["id"])
            self.assertIn("来源：NodeSeek 合集帖", action["description"], action["id"])
        # 与既有动作功能重复或来源已失效的条目不应收录。
        urls = " ".join(action["url"] for action in actions)
        for dead in ("bench.im", "git.io", "ghproxy", "DNS-Alice-Unlock"):
            self.assertNotIn(dead, urls)
        # DD 重装类脚本必须是交互向导（leitbogioro/moeclub 走 TUI 向导，fcurrk 自带菜单）。
        dd = {action["id"]: action for action in actions if action["id"].startswith("ns_dd_")}
        self.assertEqual(set(dd), {"ns_dd_leitbogioro", "ns_dd_moeclub", "ns_dd_fcurrk"})
        self.assertEqual(dd["ns_dd_leitbogioro"]["mode"], "dd")
        self.assertEqual(dd["ns_dd_leitbogioro"]["dd_variant"], "leitbogioro")
        self.assertEqual(dd["ns_dd_moeclub"]["mode"], "dd")
        self.assertEqual(dd["ns_dd_moeclub"]["dd_variant"], "moeclub")
        self.assertNotIn("prompt_args", dd["ns_dd_fcurrk"])

    def test_online_actions_declare_interaction_model(self):
        """每个 online 动作必须声明交互形态之一（2026-09-26 全量审计的固化）：

        - mode 向导（dd / fnm）
        - prompt_args 参数输入提示
        - args 固定参数变体
        - 脚本自身交互（菜单/向导）或一键自动安装、直跑检测 —— 在 SELF_INTERACTIVE 白名单
        """
        self_interactive = {
            # 脚本自带交互菜单
            "miaomiaowu", "three_x_ui", "dd_reinstall", "singbox_233", "singbox_menu", "xray_233",
            "swap_online", "bt_panel", "onepanel", "casaos", "tcp_brutal",
            "nodeseek_tcp_multifunction", "nodeseek_tcpx", "port_traffic_dog",
            "ns_dd_fcurrk", "ns_ecs", "ns_gost", "ns_aurora", "ns_pve", "ns_argox",
            "ns_kejilion", "ns_skybox", "ns_tcp_bbr_menu", "ns_fail2ban", "ns_baota",
            "ns_dufu_aniverse", "ns_autotrace", "ns_media_native", "ns_ip_quality",
            "ns_region_check", "ns_nws", "ns_taier", "ns_tcp_quality", "ns_swap", "ns_pyinstall",
            # 一键自动安装（无需交互）
            "docker", "chsrc", "ns_bbr_v3", "lazydocker_install",
            # 直接运行的检测 / 测速
            "ns_bench", "bench_speed", "ecs", "ns_nodebench", "ns_yabs",
            "ns_media_check", "ns_sick", "ns_backtrace", "ns_speedtest",
        }
        missing = []
        for action in self.config["actions"]:
            if action.get("kind") != "online":
                continue
            if action.get("mode") or action.get("prompt_args") or action.get("args"):
                continue
            if action["id"] not in self_interactive:
                missing.append(action["id"])
        self.assertEqual(missing, [])
        # 白名单内的动作必须真实存在，防止清单腐化。
        online_ids = {action["id"] for action in self.config["actions"] if action.get("kind") == "online"}
        stale = self_interactive - online_ids
        self.assertEqual(stale, set())

    def test_no_runtime_urls_point_to_dandan8511(self):
        # 仓库迁移守门：运行时会访问的文件一律不得再指向 dandan8511。
        # （nginx-ui 镜像仓库 dandan8511/nginx-ui 属另一项目，如需迁移见 README。）
        runtime_files = (
            "launch.sh", "scripts.json", "scripts/fscarmen-warp.sh",
            "scripts/geosite/update.sh", "tools/nft-forward/install.sh",
            "tools/nginx-ui/install.sh", "run.sh", "tui.py",
        )
        for name in runtime_files:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("github.com/dandan8511", text, name)

    def test_nginx_stages_an_included_conf_before_replacing(self):
        source = (ROOT / "nginx_manager.py").read_text(encoding="utf-8")
        self.assertIn("yjl-tui-stage-", source)
        self.assertIn("certbot.timer", source)
        self.assertIn('["certbot", "renew", "--dry-run"]', source)

    def test_tcp_brutal_is_first_local_action(self):
        actions = [action for action in self.config["actions"] if action.get("category") == "tcp_tuning"]
        self.assertEqual(actions[0]["id"], "tcp_brutal_install")
        self.assertEqual(actions[0]["kind"], "local_script")
        self.assertEqual(actions[0]["path"], "scripts/install-tcp-brutal.sh")
        self.assertTrue((ROOT / actions[0]["path"]).is_file())
        for protocol in ("ShadowTLS", "Shadowsocks", "Trojan", "VMess + WS", "VLESS + WS + TLS", "H2 + Reality", "gRPC + Reality"):
            self.assertIn(protocol, actions[0]["description"])

    def test_tcp_brutal_category_has_install_and_manage_actions(self):
        categories = {category["id"]: category["title"] for category in self.config["categories"]}
        self.assertEqual(categories.get("tcp_brutal"), "tcp-brutal")
        actions = [action for action in self.config["actions"] if action.get("category") == "tcp_brutal"]
        self.assertEqual([action["id"] for action in actions], ["tcp_brutal_online", "tcp_brutal_offline", "tcp_brutal_manage"])
        for action, mode in zip(actions, ("online", "offline", "manage")):
            self.assertEqual(action["kind"], "local_script")
            self.assertEqual(action["path"], "scripts/tcp-brutal-manager.sh")
            self.assertEqual(action["args"], [mode])
            self.assertTrue(action["needs_root"])

        manager = ROOT / "scripts/tcp-brutal-manager.sh"
        source = manager.read_text(encoding="utf-8")
        self.assertIn("https://tcp.hy2.sh/", source)
        self.assertIn("install --local", source)
        self.assertIn("capture_live_rules", source)
        self.assertIn("ensure_persistence_if_saved_rules", source)
        self.assertIn('route" != "yes', source)
        self.assertIn("grep -x './dkms_source_tree/dkms.conf' > /dev/null", source)
        self.assertNotIn("SSH_CONNECTION", source)
        self.assertIn("本次只安装模块，不会自动添加任何规则。", source)
        self.assertIn("manage_interactively", source)
        self.assertIn("本机 brutal 管理", actions[2]["title"])
        self.assertIn('rm -f -- "$installer"', source)
        self.assertNotIn("trap 'rm -f -- \"$installer\"' RETURN", source)
        self.assertIn("systemd", source)
        self.assertIn("openrc", source)
        self.assertIn("1000", source)
        self.assertTrue((ROOT / "scripts/tcp-brutal/LICENSE").is_file())
        self.assertTrue((ROOT / "scripts/tcp-brutal/dkms.tar.gz").is_file())
        self.assertTrue((ROOT / "scripts/tcp-brutal/UPSTREAM.md").is_file())

    # windows runner 的 Git Bash 对「source 脚本 + bash 子进程」类测试行为不稳定：
    # 两次运行分别有两个不同的测试失败且无法复现/读日志（日志接口需认证）。
    # 这些是 Linux 脚本行为测试，ubuntu CI 是覆盖主路径，本地 Git Bash 亦验证通过。
    WINDOWS_CI_BASH_SUBPROCESS_SKIP = unittest.skipIf(
        os.environ.get("CI") == "true" and os.name == "nt",
        "windows runner 的 bash 子进程行为不稳定；ubuntu CI 为该 Linux 脚本的覆盖主路径",
    )

    @WINDOWS_CI_BASH_SUBPROCESS_SKIP
    def test_tcp_brutal_manager_validates_and_normalizes_ipv4_prefixes(self):
        script = "source scripts/tcp-brutal-manager.sh; normalize_prefix 188.165.226.219; normalize_prefix 1.2.3.4/24; ! normalize_prefix 1.2.3.999/32; ! normalize_prefix 1.2.3.4/33; is_rate 1000; ! is_rate 0"
        result = subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["188.165.226.219/32", "1.2.3.4/24"])

    @WINDOWS_CI_BASH_SUBPROCESS_SKIP
    def test_tcp_brutal_online_cleanup_keeps_installer_in_function_scope(self):
        script = r'''
source scripts/tcp-brutal-manager.sh
capture_live_rules() { :; }
ensure_persistence_if_saved_rules() { :; }
configure_routes() { :; }
curl() {
    local output=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "-o" ]; then
            output="$2"
            shift 2
        else
            shift
        fi
    done
    printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$output"
}
run_online_install
'''
        result = subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_launcher_caches_tcp_brutal_offline_runtime_assets(self):
        files = dict(launch_file_list())
        for relative, mode in (
            ("scripts/tcp-brutal-manager.sh", "700"),
            ("scripts/tcp-brutal/scripts/install_dkms.sh", "700"),
            ("scripts/tcp-brutal/dkms.tar.gz", "600"),
        ):
            self.assertEqual(files.get(relative), mode)

    def test_tcpfit_is_vendored_tcp_menu_entry(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        self.assertNotIn("tcp_exit", actions)
        action = actions["tcpfit"]
        self.assertEqual(action["category"], "tcp_tuning")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "scripts/tcpfit/tcpfit.sh")
        self.assertEqual(action["interpreter"], "bash")
        self.assertTrue(action["needs_root"])
        self.assertIn("99.", action["title"])

        snapshot = ROOT / action["path"]
        self.assertTrue(snapshot.is_file())
        source = snapshot.read_text(encoding="utf-8")
        self.assertIn('VERSION="0.5.3"', source)
        self.assertIn('STATE_DIR="/var/lib/tcpfit"', source)

    def test_nodeseek_menu_actions(self):
        categories = {category["id"]: category["title"] for category in self.config["categories"]}
        self.assertEqual(categories.get("nodeseek"), "Nodeseek论坛")
        actions = [action for action in self.config["actions"] if action.get("category") == "nodeseek"]
        self.assertEqual([action["id"] for action in actions], [
            "nodeseek_bbr", "nodeseek_tcp_multifunction", "nodeseek_tcpx", "nodeseek_window_tuning",
        ])
        self.assertEqual(actions[0]["tcp_profile"], "nodeseek-bbr")
        self.assertTrue(all(action["needs_root"] for action in actions))
        window_tuning = actions[-1]
        self.assertEqual(window_tuning["kind"], "local_script")
        self.assertEqual(window_tuning["path"], "scripts/nekoneko-tools.sh")
        self.assertTrue((ROOT / window_tuning["path"]).is_file())

    def test_fscarmen_singbox_full_local_clone(self):
        categories = {category["id"]: category["title"] for category in self.config["categories"]}
        self.assertEqual(categories.get("fscarmen_singbox"), "sing-box(fsr)")
        action = next(action for action in self.config["actions"] if action["id"] == "fscarmen_singbox_menu")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "scripts/fscarmen-sing-box.sh")
        self.assertTrue(action["needs_root"])
        self.assertIn("bash <(wget -qO- fscarmen/sing-box.sh)", action["title"])

        snapshot = ROOT / action["path"]
        self.assertTrue(snapshot.is_file())
        source = snapshot.read_text(encoding="utf-8")
        self.assertIn("VERSION='v1.3.22 (2026.08.11)'", source)
        self.assertIn('PROTOCOL_LIST=("XTLS + reality"', source)
        self.assertNotIn("YJL-TUI", source)
        self.assertNotIn("--YJL-TUI-VLESS-WS-TLS", source)

    def test_fscarmen_tcp_brutal_menu9_is_local_clone(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["fscarmen_tcp_brutal_install"]
        self.assertEqual(action["category"], "fscarmen_singbox")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "scripts/install-tcp-brutal.sh")
        self.assertEqual(action["args"], [])
        self.assertTrue((ROOT / action["path"]).is_file())
        self.assertIn("菜单第 9 项", action["description"])
        self.assertNotIn("tcp.hy2.sh", action.get("url", ""))

    def test_geosite_update_is_a_vendored_fscarmen_action(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["geosite_update"]
        self.assertEqual(action["category"], "fscarmen_singbox")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "scripts/geosite/update.sh")
        self.assertEqual(action["args"], ["--sync"])
        self.assertTrue(action["needs_root"])

        updater = ROOT / action["path"]
        self.assertTrue(updater.is_file())
        source = updater.read_text(encoding="utf-8")
        self.assertIn("SagerNet/sing-geosite", source)
        self.assertIn("--vendor", source)
        self.assertIn("--sync", source)
        self.assertIn("MIRROR_RAW", source)
        self.assertIn("rule-set.tar.gz", source)
        self.assertIn("local_vendor_rule", source)
        self.assertIn('mkdir -p -- "$extracted"', source)
        self.assertTrue((ROOT / "scripts/geosite/SHA256SUMS").is_file())
        self.assertTrue((ROOT / "scripts/geosite/UPSTREAM.json").is_file())

    def test_geosite_vendor_snapshot_has_a_complete_manifest(self):
        geosite = ROOT / "scripts/geosite"
        metadata = json.loads((geosite / "UPSTREAM.json").read_text(encoding="utf-8"))
        rules = sorted((geosite / "rule-set").rglob("*.srs"))
        manifest = [line for line in (geosite / "SHA256SUMS").read_text(encoding="utf-8").splitlines() if line]
        self.assertGreater(len(rules), 100)
        self.assertEqual(metadata["rule_set_count"], len(rules))
        self.assertEqual(len(manifest), len(rules))
        for name in ("geosite-openai.srs", "geosite-anthropic.srs", "geosite-google-gemini.srs"):
            self.assertTrue((geosite / "rule-set" / name).is_file())

    def test_launcher_downloads_geosite_update_files(self):
        files = dict(launch_file_list())
        for relative, mode in (
            ("scripts/geosite/update.sh", "700"),
            ("scripts/geosite/SHA256SUMS", "600"),
            ("scripts/geosite/UPSTREAM.json", "600"),
            ("scripts/geosite/rule-set.tar.gz", "600"),
        ):
            self.assertEqual(files.get(relative), mode)

    def test_singbox_manager_is_a_cached_local_action(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["singbox_manager"]
        self.assertEqual(action["category"], "fscarmen_singbox")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "singbox_manager.py")
        self.assertEqual(action["interpreter"], "python3")
        self.assertTrue(action["needs_root"])
        self.assertTrue((ROOT / action["path"]).is_file())

        files = dict(launch_file_list())
        self.assertEqual(files.get("singbox_manager.py"), "600")

    def test_fscarmen_warp_local_clone_updates_from_this_repository(self):
        action = next(action for action in self.config["actions"] if action["id"] == "warp")
        source = (ROOT / action["path"]).read_text(encoding="utf-8")
        self.assertIn("VERSION='3.2.7'", source)
        self.assertIn("YJL_WARP_UPDATE_URL", source)
        self.assertIn(
            "https://raw.githubusercontent.com/xiaofeixoa/dandan-tui/main/scripts/fscarmen-warp.sh",
            source,
        )

    def test_docker_mirror_switch_is_local_docker_menu_entry(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["docker_mirror_switch"]
        self.assertEqual(action["category"], "docker_manage")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "scripts/docker-mirror-switch.sh")
        self.assertEqual(action["title"], "16. 国内 Docker 源检测")
        self.assertTrue(action["needs_root"])
        self.assertTrue((ROOT / action["path"]).is_file())

    def test_dockerhub_mirror_management_contract(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["dockerhub_mirror"]
        self.assertEqual(action["category"], "docker_manage")
        self.assertEqual(action["path"], "scripts/dockerhub-mirror.sh")
        source = (ROOT / action["path"]).read_text(encoding="utf-8")
        for flag in ("--cache-list", "--delete-repository", "--clear-cache", "--configure-policy", "--policy-run"):
            self.assertIn(flag, source)
        self.assertIn("garbage-collect", source)
        self.assertIn("dockerhub-mirror-cleanup.timer", source)

    def test_nft_forward_is_vendored_advanced_menu_entry(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["nft_forward"]
        self.assertEqual(action["category"], "advanced")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "tools/nft-forward/install.sh")
        self.assertTrue(action["needs_root"])

        snapshot = ROOT / action["path"]
        self.assertTrue(snapshot.is_file())
        source = snapshot.read_text(encoding="utf-8")
        self.assertIn('REPO="xjetry/nft-forward"', source)
        self.assertIn('SCRIPT_REPO="${NFTF_SCRIPT_REPO:-xiaofeixoa/dandan-tui}"', source)
        self.assertIn('SCRIPT_FILE="${NFTF_SCRIPT_FILE:-tools/nft-forward/install.sh}"', source)
        self.assertIn('https://raw.githubusercontent.com/$SCRIPT_REPO/$SCRIPT_REF/$SCRIPT_FILE', source)
        self.assertIn('NFTF_RELEASE_BASE_URL="file://$LOCAL_TOOLS_DIR"', source)
        self.assertIn("fetch_local_bundle || die", source)
        self.assertIn("未回退到上游 Release", source)

        tools = ROOT / "tools/nft-forward"
        self.assertEqual(
            hashlib.sha256((tools / "nft-agent").read_bytes()).hexdigest(),
            "c7b0844a436a33e65ebfac7e18e29f0a6914e11d36737be1af49786a748aacec",
        )
        self.assertEqual(
            hashlib.sha256((tools / "nft-server").read_bytes()).hexdigest(),
            "669958f3fe02ef4c5e29deb109e3f8a5b57cb1fedefdfb895e0e416d01c73854",
        )
        self.assertEqual(
            hashlib.sha256((tools / "SHA256SUMS").read_bytes()).hexdigest(),
            "aff9af7c899cef812615815222df18bf6379782ed3ac8a567e813e2f4d21eb34",
        )

        files = {path for path, _ in launch_file_list()}
        self.assertIn("tools/nft-forward/install.sh", files)
        self.assertNotIn("tools/nft-forward/nft-agent", files)
        self.assertNotIn("tools/nft-forward/nft-server", files)
        self.assertNotIn("tools/nft-forward/SHA256SUMS", files)

    def test_lscpu_parser_and_cpu_profile_fields(self):
        sample = """Architecture: x86_64
CPU(s): 4
Vendor ID: GenuineIntel
Model name: Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz
CPU family: 6
Model: 165
Stepping: 5
Thread(s) per core: 1
Core(s) per socket: 4
Socket(s): 1
Hypervisor vendor: VMware
Virtualization type: full
L1d cache: 128 KiB (4 instances)
Flags: fpu vmx avx2
"""
        parsed = tui.parse_lscpu(sample)
        self.assertEqual(parsed["Model name"], "Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz")
        self.assertEqual(parsed["Hypervisor vendor"], "VMware")
        self.assertEqual(parsed["Flags"], "fpu vmx avx2")

        profile = tui.cpu_hardware_profile()
        for field in ("逻辑 CPU", "CPU 架构", "CPU 厂商", "CPU 型号", "CPU 虚拟化支持", "指令集"):
            self.assertIn(field, profile)
            self.assertTrue(profile[field])

    def test_yjl_argo_is_local_advanced_menu_entry_and_launcher_cached(self):
        actions = {action["id"]: action for action in self.config["actions"]}
        action = actions["yjl_argo"]
        self.assertEqual(action["category"], "advanced")
        self.assertEqual(action["kind"], "local_script")
        self.assertEqual(action["path"], "yjl-argo/yjl-argo.sh")
        self.assertEqual(action["interpreter"], "bash")
        self.assertTrue(action["needs_root"])

        source = (ROOT / action["path"]).read_text(encoding="utf-8")
        self.assertIn("Cloudflare Tunnel", source)
        self.assertIn("systemd", source)
        self.assertIn("openrc", source)

        files = dict(launch_file_list())
        self.assertEqual(files.get("yjl-argo/yjl-argo.sh"), "700")

    def test_virtualization_profile_has_detection_and_dmi_fields(self):
        profile = tui.virtualization_profile({"虚拟化厂商": "未检测到", "指令集": ""})
        for field in ("虚拟化环境", "运行形态", "虚拟机检测", "容器检测", "DMI 产品型号", "宿主机 CPU 读取"):
            self.assertIn(field, profile)
            self.assertTrue(profile[field])

    def test_tcp_brutal_repair_only_targets_supported_nodes(self):
        jsonc = '''{
  "inbounds": [{
    "type": "vless",
    "tag": "jp vless-ws-tls",
    "transport": {"type": "ws"},
    "multiplex": {"enabled": false}
  }]
}'''
        patched, changed = tui.TUI._tcp_brutal_patch_jsonc_inbound(jsonc)
        self.assertTrue(changed)
        self.assertIn('"enabled": true', patched)
        self.assertIn('"brutal": {', patched)

        singbox = {
            "outbounds": [
                {"type": "vless", "tag": "jp vless-ws-tls", "transport": {"type": "ws"}, "multiplex": {"enabled": False}},
                {"type": "vless", "tag": "jp xtls-reality", "flow": "xtls-rprx-vision", "multiplex": {"enabled": False}},
                {"type": "vless", "tag": "jp h2-reality", "transport": {"type": "http"}},
            ]
        }
        patched, changed = tui.TUI._tcp_brutal_patch_singbox_subscription(
            json.dumps(singbox), {"jp vless-ws-tls", "jp xtls-reality", "jp h2-reality"}
        )
        self.assertTrue(changed)
        repaired = json.loads(patched)
        self.assertTrue(repaired["outbounds"][0]["multiplex"]["brutal"]["enabled"])
        self.assertFalse(repaired["outbounds"][1]["multiplex"]["enabled"])
        self.assertTrue(repaired["outbounds"][2]["multiplex"]["brutal"]["enabled"])

        yaml = '''- name: jp vless-ws-tls
  type: vless
  network: ws
  smux:
    enabled: false
  brutal-opts:
    enabled: false
- name: jp xtls-reality
  type: vless
  network: tcp
  flow: xtls-rprx-vision
  smux:
    enabled: false
  brutal-opts:
    enabled: false
'''
        patched, changed = tui.TUI._tcp_brutal_patch_yaml_subscription(yaml, {"jp vless-ws-tls", "jp xtls-reality"})
        self.assertTrue(changed)
        self.assertIn("jp vless-ws-tls\n  type: vless\n  network: ws\n  smux:\n    enabled: true", patched)
        self.assertIn("jp xtls-reality\n  type: vless\n  network: tcp\n  flow: xtls-rprx-vision\n  smux:\n    enabled: false", patched)

        missing = '''proxies:
  - name: jp vless-ws-tls
    type: vless
    network: ws
    smux:
      enabled: true
rules:
  - DOMAIN-SUFFIX,example.com,DIRECT
'''
        patched, changed = tui.TUI._tcp_brutal_patch_yaml_subscription(missing, {"jp vless-ws-tls"})
        self.assertTrue(changed)
        self.assertIn("    brutal-opts:\n      enabled: true", patched)
        self.assertIn("rules:\n  - DOMAIN-SUFFIX,example.com,DIRECT", patched)


class LocalBehaviorTests(unittest.TestCase):
    def test_shell_and_python_syntax(self):
        subprocess.run(["bash", "-n", "launch.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "run.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/install-tcp-brutal.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/tcp-brutal-manager.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/tcp-brutal/install-local.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/tcp-brutal/scripts/install_dkms.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/nekoneko-tools.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/fscarmen-sing-box.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/fscarmen-warp.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/tcpfit/tcpfit.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/docker-mirror-switch.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/dockerhub-mirror.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/kernel-installer/kernel_installer.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "yjl-argo/yjl-argo.sh"], cwd=ROOT, check=True)
        subprocess.run(["sh", "-n", "yjl-argo/install.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/geosite/release-pack.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/check-launch-manifest.sh"], cwd=ROOT, check=True)
        subprocess.run(["bash", "-n", "scripts/regen-launch-manifest.sh"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "py_compile", "tui.py"], cwd=ROOT, check=True)
        for module in ("yjl_tui/paths.py", "yjl_tui/probes.py", "yjl_tui/tcp_brutal.py", "yjl_tui/doctor.py", "yjl_tui/tui.py"):
            subprocess.run([sys.executable, "-m", "py_compile", module], cwd=ROOT, check=True)

    def test_noninteractive_commands(self):
        env = {
            **__import__("os").environ,
            "YJL_TUI_CACHE_DIR": tempfile.mkdtemp(),
            "YJL_TUI_LOG_DIR": tempfile.mkdtemp(),
            "YJL_TUI_STATE_DIR": tempfile.mkdtemp(),
        }
        check = subprocess.run(["bash", "./run.sh", "--check"], cwd=ROOT, env=env, capture_output=True, text=True)
        listing = subprocess.run(["bash", "./run.sh", "--list"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0)
        self.assertIn("配置 OK", check.stdout)
        self.assertEqual(listing.returncode, 0)
        self.assertIn("warp", listing.stdout)
        self.assertIn("onepanel", listing.stdout)
        self.assertIn("fscarmen_singbox_menu", listing.stdout)
        self.assertIn("dockerhub_mirror", listing.stdout)
        self.assertIn("yjl_argo", listing.stdout)
        self.assertIn("system_kernel_maintenance", listing.stdout)
        self.assertIn("grub_manage", listing.stdout)
        self.assertNotIn("[danger]", listing.stdout)
        self.assertNotIn("[warn]", listing.stdout)
        self.assertNotIn("[safe]", listing.stdout)

    def test_input_and_name_validation(self):
        self.assertTrue(tui.TUI.docker_name("nginx:latest", image=True))
        self.assertFalse(tui.TUI.docker_name("bad name"))
        self.assertEqual(tui.safe_name("中文 action"), "action")
        self.assertIsNotNone(tui.TUI.builtin("domain_latency"))
        self.assertTrue(tui.TUI({"categories": [], "actions": []}).confirm({"risk": "danger"}))


if __name__ == "__main__":
    unittest.main()
