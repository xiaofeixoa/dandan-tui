"""``--doctor`` 自诊断：检查运行环境、配置完整性和 launch 清单新鲜度。

所有检查都是只读的（目录可写性测试除外，只会创建并删除一个临时文件），
结果为 ``(level, message)`` 列表，方便单测；``run_doctor()`` 负责打印并给出退出码。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .paths import APP_DIR, CACHE, LOGS, STATE, VERSION


FILES_PATTERN = re.compile(r'(?m)^\s+"([^"]+)\|([0-9]{3})",?$')


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".yjl-doctor-{os.getpid()}.tmp"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _check_launch_manifest(app_dir: Path) -> tuple[str, str]:
    """与 scripts/check-launch-manifest.sh 相同的清单校验，Python 版。"""
    launcher = app_dir / "launch.sh"
    manifest = app_dir / "SHA256SUMS"
    if not launcher.is_file() or not manifest.is_file():
        return "info", "未发现 launch.sh / SHA256SUMS，跳过 launch 清单校验（安装缓存目录属正常）。"
    files = FILES_PATTERN.findall(launcher.read_text(encoding="utf-8"))
    if not files:
        return "fail", "launch.sh 未解析到 FILES 下载清单。"
    sums: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, path = line.split(None, 1)
            sums[path.strip().lstrip("*")] = digest
    missing = [path for path, _ in files if path not in sums]
    if missing:
        return "fail", "SHA256SUMS 缺少清单文件：" + ", ".join(missing[:5])
    stale = []
    for path, digest in sums.items():
        target = app_dir / path
        if not target.is_file():
            stale.append(f"{path}（不存在）")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            stale.append(path)
    if stale:
        return "fail", "SHA256SUMS 已过期，请重新生成：" + ", ".join(stale[:5])
    return "ok", f"launch 清单与 SHA256SUMS 一致（{len(sums)} 个文件）。"


def collect_checks(
    app_dir: Path | None = None,
    cache_dir: Path | None = None,
    logs_dir: Path | None = None,
    state_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """返回全部检查结果；level ∈ {"ok", "warn", "fail", "info"}。"""
    app_dir = app_dir or APP_DIR
    cache_dir = cache_dir or CACHE
    logs_dir = logs_dir or LOGS
    state_dir = state_dir or STATE
    results: list[tuple[str, str]] = []

    # 1) Python 版本
    if sys.version_info >= (3, 9):
        results.append(("ok", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    else:
        results.append(("fail", f"Python 版本过低（{sys.version.split()[0]}），至少需要 3.9。"))

    # 2) scripts.json
    config = app_dir / "scripts.json"
    action_count = 0
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        categories = data.get("categories")
        actions = data.get("actions")
        if isinstance(categories, list) and isinstance(actions, list) and actions:
            action_count = len(actions)
            results.append(("ok", f"scripts.json：{len(categories)} 个分类、{action_count} 个动作。"))
        else:
            results.append(("fail", "scripts.json 缺少 categories 或 actions 列表。"))
    except (OSError, json.JSONDecodeError) as exc:
        results.append(("fail", f"scripts.json 读取失败：{exc}"))
        actions = []

    # 3) tcp_profiles.json
    try:
        profiles = json.loads((app_dir / "tcp_profiles.json").read_text(encoding="utf-8"))
        if isinstance(profiles, dict):
            results.append(("ok", f"tcp_profiles.json：{len(profiles)} 个方案。"))
        else:
            results.append(("fail", "tcp_profiles.json 根节点不是对象。"))
    except (OSError, json.JSONDecodeError) as exc:
        results.append(("warn", f"tcp_profiles.json 不可用，本地 TCP 方案菜单会失败：{exc}"))

    # 4) 本地脚本完整性
    missing = [
        action.get("path", "?")
        for action in (actions if isinstance(actions, list) else [])
        if action.get("kind") == "local_script" and not (app_dir / str(action.get("path", ""))).is_file()
    ]
    if missing:
        results.append(("fail", "本地脚本缺失（菜单会报未找到）：" + ", ".join(missing[:5])))
    else:
        results.append(("ok", "全部 local_script 路径存在。"))

    # 5) launch 清单新鲜度
    results.append(_check_launch_manifest(app_dir))

    # 6) 关键命令
    have_curl = shutil.which("curl") is not None
    have_wget = shutil.which("wget") is not None
    if have_curl or have_wget:
        results.append(("ok", "下载器：" + ("curl" if have_curl else "wget")))
    else:
        results.append(("warn", "curl 和 wget 都缺失，在线类动作无法下载。"))
    for tool, why in (
        ("sha256sum", "launch.sh 完整性校验"),
        ("openssl", "证书 / 延迟检测"),
        ("ss", "端口监听检测"),
        ("tar", "Nginx 备份 / geosite 回退"),
        ("bash", "本地脚本执行"),
    ):
        if shutil.which(tool):
            results.append(("ok", f"{tool} 可用（{why}）。"))
        else:
            results.append(("warn", f"缺少 {tool}（{why} 会降级或失败）。"))

    # 7) 运行目录可写
    for label, path in (("缓存", cache_dir), ("日志", logs_dir), ("状态", state_dir)):
        if _writable(path):
            results.append(("ok", f"{label}目录可写：{path}"))
        else:
            results.append(("warn", f"{label}目录不可写：{path}（相关功能会失败）"))

    # 8) 环境信息
    euid_getter = getattr(os, "geteuid", None)
    if euid_getter and euid_getter() == 0:
        results.append(("info", "当前以 root 运行，全部动作可用。"))
    else:
        results.append(("info", "当前为普通用户，标注 needs_root 的动作不可用。"))
    try:
        import curses  # noqa: F401

        results.append(("info", "curses 可用，交互菜单可启动（真实终端内运行）。"))
    except ImportError:
        results.append(("warn", "当前 Python 缺少 curses，交互菜单无法启动（Windows 需 windows-curses）。"))

    return results


def collect_network_targets(app_dir: Path | None = None) -> list[str]:
    """从 scripts.json 收集所有 online / tcp_online 动作的去重 URL（排序稳定）。"""
    app_dir = app_dir or APP_DIR
    try:
        data = json.loads((app_dir / "scripts.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    urls = {
        action["url"].strip()
        for action in data.get("actions", [])
        if action.get("kind") in ("online", "tcp_online")
        and isinstance(action.get("url"), str)
        and action["url"].strip()
    }
    return sorted(urls)


def probe_urls(urls: list[str], timeout: float = 10.0, workers: int = 8) -> list[tuple[str, str]]:
    """并发探测 URL 可达性；返回 (url, 结果) 列表，结果为 HTTP 状态码或 ERR 说明。"""
    import concurrent.futures
    import urllib.error
    import urllib.request

    def probe(url: str) -> tuple[str, str]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "yjl-tui-doctor"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(512)
            return url, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return url, f"HTTP {exc.code}"
        except Exception as exc:
            return url, f"ERR {type(exc).__name__}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(probe, urls))


def run_network_probe(out=None) -> None:
    printer = out or sys.stdout
    targets = collect_network_targets()
    if not targets:
        print("无 online 动作 URL 可探测。", file=printer)
        return
    print(f"探测 {len(targets)} 个在线动作 URL（每个最长等待 10 秒）……", file=printer)
    for url, status in probe_urls(targets):
        mark = "✅" if status.startswith("HTTP 2") or status.startswith("HTTP 3") else "❌"
        print(f"{mark} {status:<10} {url}", file=printer)


def run_doctor(out=None) -> int:
    """打印诊断结果；任一 fail 时返回 1。"""
    printer = out or sys.stdout
    print(f"yjl-tui {VERSION} 自诊断", file=printer)
    results = collect_checks()
    level_marks = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "info": "ℹ️ "}
    for level, message in results:
        print(f"{level_marks.get(level, '·')} {message}", file=printer)
    failed = sum(1 for level, _ in results if level == "fail")
    warned = sum(1 for level, _ in results if level == "warn")
    print(f"\n结果：{failed} 项失败，{warned} 项警告。", file=printer)
    return 1 if failed else 0
