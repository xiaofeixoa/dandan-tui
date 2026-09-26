"""launch.sh 全链路端到端测试（在线 HTTP 与离线目录两种获取方式）。

覆盖：下载 → SHA256SUMS 逐项校验 → staging 原子安装 → 缓存树可直接运行。
这是防止「文件漏出 FILES 清单 / 校验链断裂 / 安装阶段回退」类部署级缺陷的守门测试
（regression: yjl_tui/doctor.py 曾漏出清单导致部署后 import 崩溃）。
"""
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from yjl_tui import paths

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TOOLS = ("bash", "sha256sum", "curl")
INSTALL_MODULES = ("paths.py", "probes.py", "tcp_brutal.py", "doctor.py", "tui.py")


def bash_available() -> bool:
    return all(shutil.which(tool) for tool in REQUIRED_TOOLS)


def to_bash_path(path: Path) -> str:
    """Git Bash 下把 Windows 路径转成它喜欢的形式；Linux 原样返回。"""
    if os.name == "nt" and shutil.which("cygpath"):
        result = subprocess.run(["cygpath", "-m", str(path)], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    return str(path)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(bash_available(), "需要 bash/sha256sum/curl")
class LaunchE2ETests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.cache_root = to_bash_path(self.work / "cache")

    def run_launcher(self, launcher_path: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "XDG_CACHE_HOME": self.cache_root, "PYTHONUTF8": "1"}
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", to_bash_path(launcher_path), "--check"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=600,
        )

    def assert_installed_tree(self):
        installed = Path(self.cache_root) / "dandan-tui"
        self.assertTrue((installed / "run.sh").is_file())
        self.assertTrue((installed / "scripts.json").is_file())
        # 包内模块一个都不能少——doctor.py 曾漏出清单导致部署后 import 崩溃。
        for module in INSTALL_MODULES:
            self.assertTrue((installed / "yjl_tui" / module).is_file(), f"yjl_tui/{module} 缺失")

    def test_online_pipeline_via_local_http(self):
        """把 BASE_URL 指向本机 HTTP 服务，逐字执行 launch.sh 其余逻辑。"""
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(ROOT)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = (ROOT / "launch.sh").read_text(encoding="utf-8")
            patched = re.sub(r'BASE_URL="[^"]+"', f'BASE_URL="http://127.0.0.1:{server.server_address[1]}"', source, count=1)
            self.assertNotEqual(patched, source, "BASE_URL 替换失败")
            launcher = self.work / "launch-e2e.sh"
            launcher.write_text(patched, encoding="utf-8", newline="\n")
            result = self.run_launcher(launcher)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"yjl-tui {paths.VERSION}", result.stdout)
        self.assertIn("配置 OK", result.stdout)
        self.assert_installed_tree()

    def test_offline_pipeline_via_local_source(self):
        """YJL_TUI_LOCAL_SOURCE 离线安装：不访问网络，SHA256SUMS 校验照常执行。"""
        result = self.run_launcher(ROOT / "launch.sh", {"YJL_TUI_LOCAL_SOURCE": to_bash_path(ROOT)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"yjl-tui {paths.VERSION}", result.stdout)
        self.assertIn("配置 OK", result.stdout)
        self.assert_installed_tree()

    def test_offline_mode_rejects_missing_manifest_entry(self):
        """源目录缺任一清单文件时必须失败且不落半套缓存。"""
        broken_source = self.work / "broken-source"
        shutil.copytree(ROOT, broken_source,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
                        dirs_exist_ok=True)
        (broken_source / "yjl_tui" / "doctor.py").unlink()
        result = self.run_launcher(broken_source / "launch.sh", {"YJL_TUI_LOCAL_SOURCE": to_bash_path(broken_source)})
        self.assertNotEqual(result.returncode, 0)
        cache = Path(self.cache_root)
        self.assertFalse((cache / "dandan-tui" / "tui.py").is_file(), "失败的安装不应产出可用缓存")


if __name__ == "__main__":
    unittest.main()
