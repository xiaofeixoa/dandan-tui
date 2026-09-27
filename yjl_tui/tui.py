"""YJL Linux TUI：curses 菜单、动作执行与入口。

模块职责：
- ``TUI``       动作执行器（内置动作、在线脚本、本地脚本、TCP 方案、Docker 等）
- ``draw``/``run_ui``  curses 双栏界面
- ``main``      命令行入口（--list / --check / 交互界面）
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import curses
except ImportError:  # 非 POSIX 平台（如 Windows 开发机）：允许 import 以运行测试。
    curses = None  # type: ignore[assignment]

import kernel_manager
# 以下再导出供入口薄壳 `from yjl_tui.tui import *` 暴露兼容 API（如 tui.nginx_manager.parse_sites）。
import nginx_manager  # noqa: F401
from nginx_manager import run_nginx_manager

from . import tcp_brutal
from .doctor import run_doctor
from .paths import (
    APP_DIR,
    CACHE,
    CONFIG,
    DOMAIN_LATENCY_HOSTS,
    IS_ROOT,
    LOGS,
    STATE,
    TCP_PROFILES,
    VERSION,
    WORKSPACE,
    read_config,
    safe_name,
)
from .probes import (  # noqa: F401  (parse_lscpu 为再导出)
    command_output,
    cpu_hardware_profile,
    parse_lscpu,
    service_active,
    system_profile,
    terminal_notice,
    virtualization_profile,
)

PROBE_TIMEOUT = 60

# 只有少数不可逆/影响全局的内置动作需要显式确认；其余动作保持上游脚本自身交互。
CONFIRM_PROMPTS = {
    "apt_upgrade": "即将执行 apt-get upgrade -y 升级全部系统包，可能出现新内核或服务变更。",
    "swap_builtin": "即将创建 512M /swapfile 并写入 /etc/fstab（已存在活动 Swap 时会跳过）。",
    "docker_prune": "即将 docker system prune --all：删除所有停止的容器、未使用网络、悬空镜像和构建缓存。",
}


def filter_actions(actions: list[dict], query: str) -> list[dict]:
    """按关键字过滤动作（匹配 id / 标题 / 描述，大小写不敏感）；空关键字返回空列表。"""
    q = query.strip().lower()
    if not q:
        return []
    return [
        action
        for action in actions
        if q in action.get("id", "").lower()
        or q in action.get("title", "").lower()
        or q in action.get("description", "").lower()
    ]


def pause() -> None:
    input("\n按 Enter 返回菜单...")


def ask(prompt: str, default: str = "1") -> str:
    return input(f"{prompt} [{default}]: ").strip() or default


class TUI:
    RECENT_CATEGORY_ID = "__recent__"
    SEARCH_CATEGORY_ID = "__search__"
    RECENT_LIMIT = 12

    # 动作 id → 方法名；dispatch 时用 getattr 惰性解析，替换方法/打桩都能生效。
    HANDLER_NAMES = {
        "system_info": "system_info",
        "apt_upgrade": "system_upgrade",
        "log_manage": "log_manage",
        "kernel_manage": "kernel_manage",
        "system_kernel_maintenance": "system_kernel_maintenance",
        "ssl_manage": "ssl_manage",
        "nginx_manager": "_nginx_manager_action",
        "network_manage": "network_manage",
        "grub_manage": "grub_manage",
        "ip_preference": "ip_preference",
        "webdav_manage": "webdav_manage",
        "ssh_config": "ssh_config",
        "tcp_status": "tcp_status",
        "tcp_brutal_repair": "tcp_brutal_repair",
        "tcp_remove_all": "tcp_remove_all",
        "docker_status": "docker_status",
        "docker_containers": "docker_containers",
        "docker_images": "docker_images",
        "docker_logs": "docker_logs",
        "docker_start": "_docker_start_action",
        "docker_stop": "_docker_stop_action",
        "docker_restart": "_docker_restart_action",
        "docker_exec": "docker_exec",
        "docker_pull": "docker_pull",
        "docker_remove": "docker_remove",
        "docker_compose": "docker_compose",
        "docker_prune": "docker_prune",
        "docker_daemon_restart": "docker_daemon_restart",
        "lazydocker": "lazydocker",
    }

    def __init__(self, config: dict):
        self.real_categories = list(config["categories"])
        self.actions = config["actions"]
        self.search_query = ""
        self.search_matches: list[dict] = []
        # 「最近使用」/「搜索」是挂在真实分类前的虚拟分类，rebuild_categories 统一组装。
        self.categories = list(self.real_categories)
        self.category = 0
        self.selected = 0
        self.should_exit = False
        self.status = "方向键选择，Enter 执行，Tab/左右切换分类，/ 搜索，q 退出"
        self.rebuild_categories()

    # ---------- 虚拟分类：最近使用 / 搜索 ----------

    def usage_path(self) -> Path:
        return STATE / "usage.json"

    def recent_ids(self, limit: int = RECENT_LIMIT) -> list[str]:
        """按最近使用时间排序的动作 id；文件缺失或损坏时返回空。"""
        try:
            data = json.loads(self.usage_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        items = sorted(data.items(), key=lambda kv: str(kv[1].get("last", "")), reverse=True)
        known = {a["id"] for a in self.actions}
        return [k for k, _ in items if k in known][:limit]

    def record_usage(self, action_id: str) -> None:
        """记录动作使用次数与最近时间；任何失败都不影响动作执行。"""
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            usage_file = self.usage_path()
            try:
                data = json.loads(usage_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            entry = data.get(action_id)
            count = entry["count"] + 1 if isinstance(entry, dict) and isinstance(entry.get("count"), int) else 1
            data[action_id] = {"count": count, "last": time.strftime("%Y-%m-%dT%H:%M:%S")}
            tmp = usage_file.with_name(f".{usage_file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, usage_file)
        except OSError:
            pass

    def rebuild_categories(self) -> None:
        """组装 虚拟分类（最近使用/搜索）+ 真实分类，并尽量保持当前分类选中。"""
        current_cid = self.categories[self.category]["id"] if self.categories else None
        extras = []
        if self.actions and self.recent_ids():
            extras.append({"id": self.RECENT_CATEGORY_ID, "title": "最近使用"})
        if self.search_query:
            extras.append({"id": self.SEARCH_CATEGORY_ID, "title": f"搜索：{self.search_query}"})
        self.categories = extras + self.real_categories
        ids = [c["id"] for c in self.categories]
        if current_cid in ids:
            self.category = ids.index(current_cid)
        else:
            self.category = 0
            self.selected = 0

    def set_search(self, query: str) -> None:
        self.search_query = query.strip()
        self.search_matches = filter_actions(self.actions, self.search_query)
        self.rebuild_categories()
        ids = [c["id"] for c in self.categories]
        if self.search_query and self.SEARCH_CATEGORY_ID in ids:
            self.category = ids.index(self.SEARCH_CATEGORY_ID)
            self.selected = 0
            self.status = f"搜索「{self.search_query}」：{len(self.search_matches)} 个匹配"
        elif not self.search_query:
            self.status = "已清除搜索"

    def current_actions(self) -> list[dict]:
        cid = self.categories[self.category]["id"]
        if cid == self.RECENT_CATEGORY_ID:
            by_id = {a["id"]: a for a in self.actions}
            return [by_id[i] for i in self.recent_ids() if i in by_id]
        if cid == self.SEARCH_CATEGORY_ID:
            return self.search_matches
        return [a for a in self.actions if a.get("category") == cid]

    def _nginx_manager_action(self, log: Path) -> int:
        del log  # nginx 管理器自带交互界面，不使用 TUI 日志
        return run_nginx_manager()

    def _docker_start_action(self, log: Path) -> int:
        return self.docker_container_action("docker_start", log)

    def _docker_stop_action(self, log: Path) -> int:
        return self.docker_container_action("docker_stop", log)

    def _docker_restart_action(self, log: Path) -> int:
        return self.docker_container_action("docker_restart", log)

    def log_file(self, action_id: str) -> Path:
        LOGS.mkdir(parents=True, exist_ok=True)
        return LOGS / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_name(action_id)}.log"

    @staticmethod
    def run(cmd: list[str], capture: bool = False, input_text: str | None = None, timeout: float | None = None):
        return subprocess.run(cmd, text=True, capture_output=capture, input=input_text, env=os.environ.copy(), timeout=timeout)

    @staticmethod
    def root_required() -> bool:
        if IS_ROOT:
            return True
        print("此功能需要 root 权限，请使用 sudo 或 root 登录后启动 TUI。")
        return False

    BACKUP_KEEP = 9

    @classmethod
    def backup_file(cls, path: Path) -> Path | None:
        if not path.exists():
            return None
        backup = path.with_name(path.name + ".yjl-tui.bak." + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        # 保留最早 1 份（restore_tcp_file 以其为「原始文件」）+ 最近 9 份，避免无限堆积。
        backups = sorted(path.parent.glob(path.name + ".yjl-tui.bak.*"))
        keep = {backups[0], *backups[-cls.BACKUP_KEEP:]}
        for old_backup in backups:
            if old_backup not in keep:
                try:
                    old_backup.unlink()
                except OSError:
                    pass
        return backup

    @staticmethod
    def restore_backup(path: Path, backup: Path | None) -> None:
        """恢复备份；没有备份时移除本次由 TUI 新建的文件。"""
        if backup:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def interfaces() -> list[str]:
        if not shutil.which("ip"):
            return []
        result = subprocess.run(["ip", "-o", "link", "show"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
        names = []
        for line in result.stdout.splitlines():
            match = re.match(r"\d+:\s+([^:]+):", line)
            if match and match.group(1) != "lo":
                names.append(match.group(1).split("@", 1)[0])
        return names

    # ---------- 系统信息 / 日志 / 系统更新 ----------

    def system_info(self, log: Path) -> int:
        print("== 系统能力 ==")
        for key, value in system_profile().items():
            print(f"{key}: {value}")
        cpu = cpu_hardware_profile()
        print("\n== CPU 硬件详情（当前系统可见） ==")
        for key, value in cpu.items():
            print(f"{key}: {value}")
        print("\n== 虚拟化详情 ==")
        for key, value in virtualization_profile(cpu).items():
            print(f"{key}: {value}")
        print("\n== 内存 ==")
        if shutil.which("free"):
            subprocess.run(["free", "-h"])
        print("\n== 网卡地址 ==")
        if shutil.which("ip"):
            subprocess.run(["ip", "-brief", "address"])
            print("\n== 路由 ==")
            subprocess.run(["ip", "route", "show"])
        print("\n== 磁盘 ==")
        subprocess.run(["df", "-hT"])
        print(f"\n数据目录：{STATE}\n日志目录：{LOGS}\n缓存目录：{CACHE}")
        return 0

    def system_upgrade(self, log: Path) -> int:
        if not self.root_required():
            return 1
        manager = system_profile()["包管理器"]
        if manager == "apt":
            result = subprocess.run(["apt-get", "update"])
            if result.returncode == 0:
                result = subprocess.run(["apt-get", "upgrade", "-y"])
            return result.returncode
        if manager == "apk":
            result = subprocess.run(["apk", "update"])
            if result.returncode == 0:
                result = subprocess.run(["apk", "upgrade"])
            return result.returncode
        print("当前系统未识别到 apt/apk，未执行系统更新。")
        return 1

    def log_manage(self, log: Path) -> int:
        print("== 日志与磁盘策略 ==")
        if LOGS.exists():
            subprocess.run(["du", "-sh", str(LOGS)])
        if shutil.which("journalctl"):
            subprocess.run(["journalctl", "--disk-usage"])
        print("\n1. 只查看占用\n2. 配置小硬盘策略（TUI 2MB/文件、最多 3 个；journal 50MB/7天）")
        choice = ask("选择")
        if choice == "1":
            return 0
        if choice != "2" or not self.root_required():
            return 1
        try:
            Path("/etc/logrotate.d").mkdir(parents=True, exist_ok=True)
            rotate = Path("/etc/logrotate.d/yjl-tui")
            rotate.write_text(
                f"{LOGS}/*.log {{\n    size 2M\n    rotate 3\n    daily\n    compress\n    missingok\n    notifempty\n    copytruncate\n}}\n",
                encoding="utf-8",
            )
            print(f"已写入：{rotate}")
            if system_profile()["初始化"] == "systemd":
                dropin = Path("/etc/systemd/journald.conf.d/99-yjl-tui.conf")
                dropin.parent.mkdir(parents=True, exist_ok=True)
                dropin.write_text(
                    "[Journal]\nSystemMaxUse=50M\nRuntimeMaxUse=20M\nMaxRetentionSec=7day\n",
                    encoding="utf-8",
                )
                subprocess.run(["systemctl", "restart", "systemd-journald"], check=False)
                subprocess.run(["journalctl", "--vacuum-size=50M", "--vacuum-time=7d"], check=False)
                print(f"已写入：{dropin}")
            else:
                print("当前不是 systemd，已配置 TUI 日志轮转；没有修改 Alpine/OpenRC 的日志服务。")
            print("策略已启用。它限制日志容量，不会关闭必要的错误日志。")
            return 0
        except OSError as exc:
            print(f"写入日志策略失败：{exc}")
            return 1

    # ---------- 内核管理 ----------

    def kernel_manage(self, log: Path) -> int:
        if not self.root_required():
            return 1
        profile = system_profile()
        print(f"系统：{profile['系统']}\n当前内核：{profile['内核']}\n包管理器：{profile['包管理器']}")
        print("\n1. 查看可用官方内核\n2. 安装指定官方内核\n3. 更新内核源并查看可用内核")
        choice = ask("选择")
        if choice not in {"1", "2", "3"}:
            return 2
        if profile["包管理器"] == "apt":
            if choice == "3":
                subprocess.run(["apt-get", "update"])
            result = subprocess.run(
                ["bash", "-c", "apt-cache search '^linux-(image|headers|modules|generic)' | sed -n '1,80p'"],
                text=True,
            )
            if choice == "1" or choice == "3":
                print("\n以上是系统源内可见的内核相关包。常见安全选择是 linux-image-amd64 或 linux-generic。")
                return result.returncode
            package = input("输入要安装的包名（例如 linux-image-amd64）：").strip()
            if not re.fullmatch(r"linux-[A-Za-z0-9.+:~-]+", package):
                print("包名格式不合法，只允许 linux- 开头的官方内核包。")
                return 2
            result = subprocess.run(["apt-get", "install", "-y", package])
        elif profile["包管理器"] == "apk":
            if choice == "3":
                subprocess.run(["apk", "update"])
            result = subprocess.run(["apk", "search", "-v", "linux-"])
            if choice == "1" or choice == "3":
                return result.returncode
            package = input("输入要安装的 Alpine 内核包名：").strip()
            if not re.fullmatch(r"linux-[A-Za-z0-9.+_-]+", package):
                print("包名格式不合法，只允许 linux- 开头的官方内核包。")
                return 2
            result = subprocess.run(["apk", "add", package])
        else:
            print("当前系统暂未识别到 apt/apk，无法安全列出官方内核。")
            return 1
        if result.returncode == 0:
            if shutil.which("update-grub"):
                subprocess.run(["update-grub"])
            print("内核包安装完成。请先确认新内核已经出现在 GRUB，再决定是否重启。")
        return result.returncode

    def system_kernel_maintenance(self, log: Path) -> int:
        if not self.root_required():
            return 1
        facts = kernel_manager.collect_kernel_facts()
        print(kernel_manager.format_kernel_report(facts))
        if reason := facts.installation_block_reason():
            print(f"\n{reason}")
            return 1
        if not shutil.which("apt-cache") or not shutil.which("apt-get"):
            print("\n当前系统没有 apt，暂不能使用系统内核维护。")
            return 1
        print(
            "\n1. 查看官方稳定内核安装计划\n2. 安装官方稳定内核\n3. 查看 Ubuntu HWE 计划\n"
            "4. Ubuntu Mainline 查询、校验和安装（amd64）\n5. 查看已安装内核与 GRUB 状态\n"
            "6. Debian Backports 内核源维护\n7. kernel.org 源码编译（高级）\n"
            "8. 安装 Debian Backports 内核"
        )
        choice = ask("选择")
        track = "hwe" if choice == "3" else "backports" if choice == "8" else "stable"
        identity = facts.identity
        if choice in {"1", "2", "3", "8"}:
            if track == "backports":
                managed_source = Path("/etc/apt/sources.list.d") / f"yjl-tui-kernel-{identity.codename}-backports.list"
                if not kernel_manager.debian_backports_source(identity) or not managed_source.is_file():
                    print("请先在第 6 项启用本 TUI 管理的 Debian Backports 源。")
                    return 1
            packages = (
                (f"linux-image-{identity.architecture}", f"linux-headers-{identity.architecture}")
                if identity.distro_id == "debian"
                else ((f"linux-generic-hwe-{identity.version_id}", f"linux-headers-generic-hwe-{identity.version_id}")
                      if track == "hwe" else ("linux-generic", "linux-headers-generic"))
            )
            candidates = {}
            for package in packages:
                apt_query = ["apt-cache", "policy", package]
                if track == "backports":
                    apt_query = ["apt-cache", "-t", f"{identity.codename}-backports", "policy", package]
                value = command_output(apt_query)
                candidate = re.search(r"(?m)^\s*Candidate:\s*(\S+)", value)
                if candidate and candidate.group(1) != "(none)":
                    candidates[package] = candidate.group(1)
            plan = kernel_manager.recommended_apt_plan(identity, candidates, track)
            if not plan:
                print("当前已配置 apt 源没有完整的对应内核元包，不执行安装。")
                return 1
            print(f"\n计划：{plan.label}\n来源：当前已配置的 apt 源\n包：{' '.join(plan.packages)}")
            if choice not in {"2", "8"}:
                return 0
            if input("输入 INSTALL 确认仅安装以上内核包：").strip() != "INSTALL":
                print("已取消。")
                return 2
            command = ["apt-get", "install", "-y", *plan.packages]
            if track == "backports":
                command = list(kernel_manager.backports_apt_arguments(identity, plan.packages) or ())
                if not command:
                    return 1
            result = subprocess.run(command)
            if result.returncode:
                return result.returncode
            if shutil.which("update-initramfs"):
                subprocess.run(["update-initramfs", "-u"], check=False)
            if shutil.which("update-grub"):
                subprocess.run(["update-grub"], check=False)
            print("内核包已安装并已尝试刷新引导。请进入“引导维护”确认新内核，再重启并用 uname -r 验证。")
            return 0
        if choice == "4":
            return self.ubuntu_mainline_maintenance(facts)
        if choice == "5":
            subprocess.run(["dpkg", "-l", "linux-image*"], check=False)
            subprocess.run(["bash", "-c", "grep -E '^menuentry|^submenu' /boot/grub/grub.cfg 2>/dev/null || true"])
            return 0
        if choice == "6":
            return self.debian_backports_maintenance(facts)
        if choice == "7":
            return self.kernel_source_build(facts, log)
        return 2

    @staticmethod
    def _download_https(url: str, target: Path) -> None:
        if not url.startswith("https://"):
            raise ValueError("只允许 HTTPS 下载")
        temporary = target.with_name("." + target.name + f".{os.getpid()}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def debian_backports_maintenance(self, facts: kernel_manager.KernelFacts) -> int:
        source = kernel_manager.debian_backports_source(facts.identity)
        if not source:
            print("Backports 源仅支持受支持的 Debian 版本；Ubuntu HWE 使用现有官方 apt 源，不创建第三方源。")
            return 1
        target = Path("/etc/apt/sources.list.d") / f"yjl-tui-kernel-{facts.identity.codename}-backports.list"
        print(f"TUI 管理的源文件：{target}\n内容：{source}")
        print("1. 查看状态\n2. 启用/更新该 TUI 源\n3. 移除该 TUI 源")
        choice = ask("选择")
        if choice == "1":
            print(target.read_text(encoding="utf-8", errors="ignore") if target.exists() else "当前未启用。")
            return 0
        if choice == "2":
            if input("输入 BACKPORTS 确认写入上述唯一源文件并执行 apt-get update：").strip() != "BACKPORTS":
                return 2
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = self.backup_file(target)
            target.write_text(source + "\n", encoding="utf-8")
            result = subprocess.run(["apt-get", "update"], check=False)
            if result.returncode:
                self.restore_backup(target, backup)
                print("apt-get update 失败，已恢复此前的 TUI 源状态。")
                return result.returncode
            subprocess.run(["apt-cache", "policy", f"linux-image-{facts.identity.architecture}"], check=False)
            return 0
        if choice == "3":
            if not target.exists():
                print("没有可移除的 TUI Backports 源。")
                return 0
            if input("输入 REMOVE 确认只移除上述 TUI 源文件：").strip() != "REMOVE":
                return 2
            self.backup_file(target)
            target.unlink()
            return subprocess.run(["apt-get", "update"], check=False).returncode
        return 2

    def ubuntu_mainline_maintenance(self, facts: kernel_manager.KernelFacts) -> int:
        identity = facts.identity
        if identity.distro_id != "ubuntu" or identity.architecture != "amd64":
            print("Ubuntu Mainline 预编译包只对 Ubuntu amd64 提供；Debian/arm64 请使用官方 apt 或源码编译路径。")
            return 1
        if "disabled" not in facts.secure_boot.lower():
            print("Secure Boot 状态不是明确 disabled。Ubuntu Mainline 包为 unsigned，拒绝自动安装。")
            return 1
        version = input("输入上游版本目录（如 v6.16.3）：").strip()
        if not re.fullmatch(r"v\d+\.\d+(?:\.\d+)?(?:-rc\d+)?", version):
            print("版本格式不合法。")
            return 2
        base_url = f"https://kernel.ubuntu.com/mainline/{version}"
        try:
            with urllib.request.urlopen(base_url + "/", timeout=30) as response:
                page = response.read().decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            print(f"无法读取 Ubuntu Mainline 页面：{exc}")
            return 1
        plan = kernel_manager.mainline_package_plan(version, page, identity.architecture)
        if not plan:
            print("该版本没有完整且测试成功的 amd64 包，未提供安装。")
            return 1
        print("可校验包：\n" + "\n".join(f"  {name}" for name in plan.packages))
        if input("输入 VERIFY 下载校验清单和包，但暂不安装：").strip() != "VERIFY":
            return 2
        cache = CACHE / "kernel-mainline" / version
        cache.mkdir(parents=True, exist_ok=True)
        try:
            self._download_https(base_url + "/CHECKSUMS", cache / "CHECKSUMS")
            self._download_https(base_url + "/CHECKSUMS.gpg", cache / "CHECKSUMS.gpg")
            keyring = APP_DIR / "scripts" / "ubuntu-mainline-signing-key.gpg"
            if not shutil.which("gpgv") or not keyring.is_file():
                print("缺少 gpgv 或本地固定的 Ubuntu Mainline 签名 keyring，拒绝继续安装。")
                return 1
            verified = subprocess.run(
                ["gpgv", "--keyring", str(keyring), str(cache / "CHECKSUMS.gpg"), str(cache / "CHECKSUMS")],
                check=False,
            )
            if verified.returncode:
                print("CHECKSUMS.gpg 验签失败，拒绝继续安装。")
                return verified.returncode
            sums = kernel_manager.mainline_sha256sums((cache / "CHECKSUMS").read_text(encoding="utf-8"), plan.packages)
            if not sums:
                print("CHECKSUMS 未覆盖全部候选包，拒绝继续安装。")
                return 1
            for package in plan.packages:
                target = cache / package
                self._download_https(base_url + "/" + package, target)
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != sums[package]:
                    print(f"SHA-256 不匹配：{package}")
                    return 1
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"下载或校验失败：{exc}")
            return 1
        if input("校验完成；输入 INSTALL 才会 dpkg 安装上列 Mainline 包：").strip() != "INSTALL":
            print("校验文件已保留，未安装。")
            return 2
        result = subprocess.run(["dpkg", "-i", *(str(cache / package) for package in plan.packages)], check=False)
        if result.returncode == 0 and shutil.which("update-grub"):
            subprocess.run(["update-grub"], check=False)
        if result.returncode == 0:
            print("Mainline 包已安装并尝试刷新 GRUB。本工具不会重启；请在引导维护确认后，重启并用 uname -r 验证。")
        return result.returncode

    def kernel_source_build(self, facts: kernel_manager.KernelFacts, log: Path) -> int:
        script = APP_DIR / "scripts" / "kernel-installer" / "kernel_installer.sh"
        if not script.is_file() or not (script.parent / "src" / "slib.sh").is_file():
            print("本地源码编译工具不完整，未执行。")
            return 1
        try:
            free_gib = shutil.disk_usage("/").free // 1024 // 1024 // 1024
        except OSError:
            free_gib = 0
        memory = command_output(["free", "-m"])
        print(
            "源码编译会下载内核和构建依赖，耗时较长，并占用大量 CPU、内存和磁盘。\n"
            f"当前根分区可用：{free_gib} GiB；内存概览：\n{memory or '无法读取'}"
        )
        if free_gib < 8:
            print("根分区可用空间低于 8 GiB，拒绝启动源码编译。")
            return 1
        print("1. kernel.org 稳定版\n2. kernel.org 长期支持版")
        choice = ask("选择")
        options = {"1": "--stable", "2": "--longterm"}
        if choice not in options:
            return 2
        if input("输入 BUILD 确认开始本地源码编译（不会自动重启或卸载旧内核）：").strip() != "BUILD":
            return 2
        return self.interactive(["bash", str(script), options[choice]], log)

    # ---------- Nginx / SSL ----------

    @staticmethod
    def nginx_config_text() -> str:
        if not shutil.which("nginx"):
            return ""
        try:
            result = subprocess.run(["nginx", "-T"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout + result.stderr if result.returncode == 0 else ""

    def nginx_domains(self) -> list[str]:
        text = self.nginx_config_text()
        domains: set[str] = set()
        for line in text.splitlines():
            line = line.split("#", 1)[0]
            match = re.search(r"\bserver_name\s+([^;]+);", line)
            if match:
                for value in match.group(1).split():
                    if re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]+", value) and "." in value:
                        domains.add(value)
        return sorted(domains)

    def write_nginx_ssl_config(self, domain: str, cert_dir: Path) -> tuple[bool, Path | None]:
        """Add certificate directives to the existing server block when it is unambiguous."""
        candidates = []
        for root in (Path("/etc/nginx"), Path("/usr/local/nginx/conf")):
            if root.is_dir():
                candidates.extend(path for path in root.rglob("*.conf") if path.is_file())
        domain_pattern = rf"(?m)^\s*server_name\s+[^;]*{re.escape(domain)}(?:\s|;)"
        path = next((candidate for candidate in sorted(set(candidates)) if re.search(domain_pattern, candidate.read_text(encoding="utf-8", errors="ignore"))), None)
        if not path:
            print(f"没有找到包含 {domain} 的 Nginx 配置文件，证书已安装但未自动改写站点配置。")
            return False, None
        original = path.read_text(encoding="utf-8", errors="surrogateescape")
        domain_line = re.search(domain_pattern, original)
        server_match = re.search(r"(?m)^\s*server\s*\{", original[: domain_line.start() + 1] if domain_line else original)
        if not server_match:
            print(f"无法定位 {domain} 所属 server 块，证书已安装但未自动改写配置。")
            return False, None
        start = server_match.start()
        depth = 0
        closing = None
        for index in range(server_match.end() - 1, len(original)):
            if original[index] == "{":
                depth += 1
            elif original[index] == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing is None:
            print("Nginx server 块括号不完整，已停止改写。")
            return False, None
        block = original[start:closing]
        cert = cert_dir / "fullchain.pem"
        key = cert_dir / "privkey.pem"
        additions = []
        if not re.search(r"(?m)^\s*listen\s+[^;]*443", block):
            additions.append("    listen 443 ssl;")
        if re.search(r"(?m)^\s*ssl_certificate\s+[^;]+;", block):
            block = re.sub(r"(?m)^\s*ssl_certificate\s+[^;]+;", f"    ssl_certificate {cert};", block, count=1)
        else:
            additions.append(f"    ssl_certificate {cert};")
        if re.search(r"(?m)^\s*ssl_certificate_key\s+[^;]+;", block):
            block = re.sub(r"(?m)^\s*ssl_certificate_key\s+[^;]+;", f"    ssl_certificate_key {key};", block, count=1)
        else:
            additions.append(f"    ssl_certificate_key {key};")
        if not re.search(r"(?m)^\s*ssl_protocols\s+", block):
            additions.append("    ssl_protocols TLSv1.2 TLSv1.3;")
        if additions:
            block = block[: block.find("\n") + 1] + "\n".join(additions) + "\n" + block[block.find("\n") + 1 :]
        updated = original[:start] + block + original[closing:]
        backup = self.backup_file(path)
        path.write_text(updated, encoding="utf-8", errors="surrogateescape")
        try:
            result = subprocess.run(["nginx", "-t"], text=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.restore_backup(path, backup)
            print(f"nginx -t 运行失败，已恢复配置：{exc}")
            return False, backup
        if result.returncode:
            self.restore_backup(path, backup)
            print(f"nginx -t 失败，已恢复配置；备份：{backup}")
            return False, backup
        subprocess.run(["nginx", "-s", "reload"], check=False, timeout=PROBE_TIMEOUT)
        return True, backup

    def ensure_acme(self, email: str, log: Path) -> Path | None:
        acme_home = Path.home() / ".acme.sh"
        acme = acme_home / "acme.sh"
        if acme.is_file():
            return acme
        if not shutil.which("curl"):
            print("缺少 curl，无法安装 acme.sh。")
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        installer = CACHE / "acme.sh"
        result = subprocess.run(
            ["curl", "-fL", "--retry", "3", "--connect-timeout", "20", "--max-time", "300",
             "https://raw.githubusercontent.com/acmesh-official/acme.sh/master/acme.sh", "-o", str(installer)],
            text=True, capture_output=True, timeout=360,
        )
        if result.returncode:
            print(result.stderr.strip() or "acme.sh 下载失败")
            return None
        installer.chmod(0o700)
        if not self.syntax_ok(installer, "bash"):
            return None
        result = subprocess.run(["bash", str(installer), "--install", "--home", str(acme_home), "--accountemail", email], text=True, timeout=900)
        if result.returncode or not acme.is_file():
            print("acme.sh 安装失败，请查看本次操作日志。")
            return None
        return acme

    def ssl_manage(self, log: Path) -> int:
        if not self.root_required():
            return 1
        if not shutil.which("nginx"):
            print("没有检测到 nginx。请先安装或确认 nginx 在 PATH 中。")
            return 1
        domains = self.nginx_domains()
        print("检测到的 Nginx 域名：")
        for index, domain in enumerate(domains, 1):
            print(f"  {index}. {domain}")
        print("\n1. 查看证书状态\n2. 申请新证书\n3. 批量续期")
        choice = ask("选择")
        if choice == "1":
            text = self.nginx_config_text()
            certs = sorted(set(re.findall(r"\bssl_certificate\s+([^;]+);", text)))
            if not certs:
                print("当前 Nginx 配置没有发现 ssl_certificate。")
            for cert in certs:
                path = Path(cert.strip())
                print(f"\n证书：{path}")
                if path.is_file():
                    subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-subject", "-issuer", "-dates"], check=False, timeout=PROBE_TIMEOUT)
                else:
                    print("文件不存在")
            return 0
        if choice == "3":
            acme = Path.home() / ".acme.sh" / "acme.sh"
            if not acme.is_file():
                print("未检测到 acme.sh 续期任务。")
                return 1
            result = subprocess.run([str(acme), "--cron", "--home", str(acme.parent)], timeout=1800)
            if result.returncode == 0:
                subprocess.run(["nginx", "-t"], check=False, timeout=PROBE_TIMEOUT)
            return result.returncode
        if choice != "2":
            return 2
        selected = input("输入域名编号（逗号分隔），或直接输入域名：").strip()
        if not selected:
            print("没有输入域名。")
            return 2
        if all(part.strip().isdigit() for part in selected.split(",")) and domains:
            picked: list[str] = []
            out_of_range: list[str] = []
            for part in selected.split(","):
                index = int(part.strip())
                if 0 < index <= len(domains):
                    picked.append(domains[index - 1])
                else:
                    out_of_range.append(part.strip())
            if out_of_range:
                print(f"忽略超出范围的编号：{', '.join(out_of_range)}")
            chosen = picked
        else:
            chosen = selected.split()
        chosen = [d for d in chosen if re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]+", d) and "." in d]
        if not chosen:
            print("域名格式不合法。")
            return 2
        print("\n域名解析预检查：")
        for domain in chosen:
            result = subprocess.run(["getent", "ahosts", domain], text=True, capture_output=True, timeout=PROBE_TIMEOUT) if shutil.which("getent") else None
            if result and result.returncode == 0:
                addresses = sorted({line.split()[0] for line in result.stdout.splitlines() if line.split()})
                print(f"  {domain}: {', '.join(addresses[:6])}")
            else:
                print(f"  {domain}: 未解析到地址，申请大概率失败")
        email = input("申请邮箱：").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            print("邮箱格式不合法。")
            return 2
        method = input("验证方式：1 HTTP-01  2 DNS-01 [1]: ").strip() or "1"
        if method not in {"1", "2"}:
            return 2
        if method == "1":
            if self.listening("80"):
                print("本机 TCP 80 已有监听，acme.sh --nginx 会尝试临时接管 Nginx；请确认外部 80 也能访问。")
            else:
                print("本机没有监听 TCP 80；HTTP-01 大概率会失败，建议使用 DNS-01。")
        env: dict[str, str] = {}
        dns_provider = ""
        if method == "2":
            dns_provider = input("DNS 服务商 [cloudflare/aliyun/tencent]：").strip().lower()
            token = getpass.getpass("DNS API Token/Secret（不会写入命令行日志）：")
            if not token:
                print("DNS 凭据为空。")
                return 2
            if dns_provider == "cloudflare":
                env["CF_Token"] = token
            elif dns_provider == "aliyun":
                env["Ali_Key"] = token.split("|", 1)[0]
                env["Ali_Secret"] = token.split("|", 1)[1] if "|" in token else getpass.getpass("Ali_Secret：")
            elif dns_provider == "tencent":
                env["Tencent_SecretId"] = token.split("|", 1)[0]
                env["Tencent_SecretKey"] = token.split("|", 1)[1] if "|" in token else getpass.getpass("Tencent_SecretKey：")
            else:
                print("目前内置 DNS API 仅支持 cloudflare、aliyun、tencent；可先手动配置 acme.sh。")
                return 2
        acme = self.ensure_acme(email, log)
        if not acme:
            return 1
        nginx_dir = Path("/etc/nginx")
        backup = STATE / ("nginx-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz")
        STATE.mkdir(parents=True, exist_ok=True)
        if nginx_dir.is_dir():
            subprocess.run(["tar", "-czf", str(backup), "-C", "/etc", "nginx"], check=False, timeout=300)
            print(f"Nginx 配置备份：{backup}")
        primary = chosen[0].replace("*.", "wildcard-")
        cert_dir = Path("/etc/ssl/yjl-tui") / primary
        cert_dir.mkdir(parents=True, exist_ok=True)
        issue = [str(acme), "--issue", "--home", str(acme.parent)]
        for domain in chosen:
            issue.extend(["-d", domain])
        if method == "1":
            issue.append("--nginx")
        else:
            issue.extend(["--dns", {"cloudflare": "dns_cf", "aliyun": "dns_ali", "tencent": "dns_tencent"}[dns_provider]])
        issue.extend(["--server", "letsencrypt"])
        result = subprocess.run(issue, env={**os.environ, **env}, timeout=1800)
        if result.returncode:
            print("证书申请失败，Nginx 原配置尚未写入。")
            return result.returncode
        install = [str(acme), "--install-cert", "-d", chosen[0], "--home", str(acme.parent), "--key-file", str(cert_dir / "privkey.pem"), "--fullchain-file", str(cert_dir / "fullchain.pem"), "--reloadcmd", "nginx -t && nginx -s reload"]
        result = subprocess.run(install, env={**os.environ, **env}, timeout=1800)
        if result.returncode:
            print("证书已申请，但安装到 Nginx 失败；请检查备份和证书目录。")
            return result.returncode
        if input("自动写入现有 Nginx server 块并 reload？[Y/n] ").strip().lower() not in {"n", "no"}:
            configured, config_backup = self.write_nginx_ssl_config(chosen[0], cert_dir)
            if config_backup:
                print(f"Nginx 配置备份：{config_backup}")
            if not configured:
                print("证书申请和安装已完成，但请手动确认 server_name 对应站点的 SSL 指令。")
        print(f"证书路径：{cert_dir}\n续期方式：acme.sh cron\nNginx 配置备份：{backup}")
        return 0

    # ---------- 网络 ----------

    def network_backend(self, iface: str) -> str:
        """Detect the active manager for this interface, not merely installed commands."""
        if shutil.which("nmcli"):
            try:
                connection = subprocess.run(
                    ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface],
                    text=True, capture_output=True, timeout=PROBE_TIMEOUT,
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                connection = ""
            if connection and connection != "--" and (
                service_active("NetworkManager.service") or not shutil.which("systemctl")
            ):
                return "NetworkManager"
        if service_active("systemd-networkd.service") and shutil.which("networkctl"):
            return "systemd-networkd"
        if service_active("networking.service") and Path("/etc/network/interfaces").is_file():
            return "ifupdown"
        if shutil.which("rc-service") and Path("/etc/network/interfaces").is_file():
            status = subprocess.run(["rc-service", "networking", "status"], check=False, timeout=PROBE_TIMEOUT)
            if status.returncode == 0:
                return "OpenRC networking"
        if shutil.which("netplan") and Path("/etc/netplan").is_dir():
            return "netplan"
        return "未识别"

    @staticmethod
    def current_network_values(iface: str) -> tuple[str, str, list[str]]:
        address = ""
        gateway = ""
        dns: list[str] = []
        if shutil.which("ip"):
            addr = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"],
                text=True, capture_output=True, timeout=PROBE_TIMEOUT,
            ).stdout
            match = re.search(r"\binet\s+(\S+)", addr)
            address = match.group(1) if match else ""
            routes = subprocess.run(
                ["ip", "-4", "route", "show", "default", "dev", iface],
                text=True, capture_output=True, timeout=PROBE_TIMEOUT,
            ).stdout
            match = re.search(r"\bvia\s+(\S+)", routes)
            gateway = match.group(1) if match else ""
        resolv = Path("/etc/resolv.conf")
        if resolv.is_file():
            for line in resolv.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.match(r"\s*nameserver\s+(\S+)", line)
                if match and match.group(1) not in {"127.0.0.53", "127.0.0.1", "::1"}:
                    dns.append(match.group(1))
        if not dns and shutil.which("resolvectl"):
            result = subprocess.run(["resolvectl", "dns", iface], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
            for item in result.stdout.split(":", 1)[-1].split():
                if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", item):
                    dns.append(item)
        return address, gateway, dns

    @staticmethod
    def network_prompt(current: tuple[str, str, list[str]]) -> tuple[str, str, list[str]] | None:
        import ipaddress

        old_address, old_gateway, old_dns = current
        print("直接按 Enter 保留当前值；输入 - 可清空网关或 DNS。")
        cidr = input(f"IPv4 地址/掩码 [{old_address or '必填，例如 10.0.1.30/24'}]: ").strip() or old_address
        gateway_value = input(f"IPv4 网关 [{old_gateway or '无'}]: ").strip()
        gateway = old_gateway if not gateway_value else ("" if gateway_value == "-" else gateway_value)
        dns_value = input(f"DNS，多个用逗号分隔 [{','.join(old_dns) or '无'}]: ").strip()
        dns_text = ",".join(old_dns) if not dns_value else ("" if dns_value == "-" else dns_value)
        dns = [item.strip() for item in dns_text.split(",") if item.strip()]
        try:
            address = ipaddress.ip_interface(cidr)
            if address.version != 4 or (gateway and ipaddress.ip_address(gateway).version != 4):
                raise ValueError
            for item in dns:
                if ipaddress.ip_address(item).version != 4:
                    raise ValueError
        except (ValueError, TypeError):
            print("IPv4 地址、网关或 DNS 格式不合法。")
            return None
        return cidr, gateway, dns

    def apply_network_config(self, iface: str, backend: str, cidr: str, gateway: str, dns: list[str]) -> int:
        if backend == "NetworkManager":
            try:
                connection = subprocess.run(
                    ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface],
                    text=True, capture_output=True, timeout=PROBE_TIMEOUT,
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                connection = ""
            if not connection or connection == "--":
                print("没有找到该网卡对应的 NetworkManager 连接。")
                return 1
            command = ["nmcli", "connection", "modify", connection, "ipv4.method", "manual", "ipv4.addresses", cidr, "ipv4.gateway", gateway]
            command.extend(["ipv4.dns", ",".join(dns)] if dns else ["ipv4.dns", "", "ipv4.ignore-auto-dns", "no"])
            result = subprocess.run(command)
            if result.returncode == 0:
                print("配置已保存，正在重新激活 NetworkManager 连接；SSH 可能会断开。")
                result = subprocess.run(["nmcli", "connection", "up", connection])
            return result.returncode

        if backend == "netplan":
            if not shutil.which("netplan"):
                print("未找到 netplan 命令，未修改。")
                return 1
            path = Path("/etc/netplan/99-yjl-tui.yaml")
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = self.backup_file(path)
            content = f"network:\n  version: 2\n  ethernets:\n    {iface}:\n      dhcp4: false\n      addresses: [{cidr}]\n      accept-ra: true\n"
            if gateway:
                content += f"      routes:\n        - to: default\n          via: {gateway}\n"
            if dns:
                content += "      nameservers:\n        addresses: [" + ", ".join(dns) + "]\n"
            path.write_text(content, encoding="utf-8")
            result = subprocess.run(["netplan", "generate"])
            if result.returncode == 0:
                print("配置校验通过，正在应用 netplan；SSH 可能会断开。")
                result = subprocess.run(["netplan", "apply"])
            if result.returncode:
                self.restore_backup(path, backup)
                print("网络配置失败，已恢复原配置。")
            else:
                print(f"配置已写入：{path}" + (f"；备份：{backup}" if backup else ""))
            return result.returncode

        if backend == "systemd-networkd":
            path = Path("/etc/systemd/network") / f"20-yjl-tui-{safe_name(iface)}.network"
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = self.backup_file(path)
            content = f"[Match]\nName={iface}\n\n[Network]\nAddress={cidr}\nIPv6AcceptRA=yes\n"
            if gateway:
                content += f"Gateway={gateway}\n"
            for item in dns:
                content += f"DNS={item}\n"
            path.write_text(content, encoding="utf-8")
            result = subprocess.run(["networkctl", "reload"])
            if result.returncode == 0:
                print("配置已保存，正在重新配置网卡；SSH 可能会断开。")
                result = subprocess.run(["networkctl", "reconfigure", iface])
            if result.returncode:
                self.restore_backup(path, backup)
                print("网络配置失败，已恢复原配置。")
            else:
                print(f"配置已写入：{path}" + (f"；备份：{backup}" if backup else ""))
            return result.returncode

        if backend in {"ifupdown", "OpenRC networking"}:
            path = Path("/etc/network/interfaces.d") / f"yjl-tui-{safe_name(iface)}"
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = self.backup_file(path)
            import ipaddress
            netmask = str(ipaddress.ip_interface(cidr).network.netmask)
            content = f"auto {iface}\niface {iface} inet static\n    address {cidr.split('/', 1)[0]}\n    netmask {netmask}\n"
            if gateway:
                content += f"    gateway {gateway}\n"
            if dns:
                content += f"    dns-nameservers {' '.join(dns)}\n"
            path.write_text(content, encoding="utf-8")
            main = Path("/etc/network/interfaces")
            main_backup = self.backup_file(main)
            original = main.read_text(encoding="utf-8", errors="surrogateescape") if main.exists() else ""
            source_line = "source /etc/network/interfaces.d/*"
            if source_line not in original:
                main.write_text(original.rstrip() + "\n\n" + source_line + "\n", encoding="utf-8", errors="surrogateescape")
            if backend == "OpenRC networking":
                result = subprocess.run(["rc-service", "networking", "restart"])
            elif shutil.which("systemctl"):
                result = subprocess.run(["systemctl", "restart", "networking"], check=False)
            else:
                result = subprocess.CompletedProcess([], 0)
            if result.returncode:
                self.restore_backup(path, backup)
                self.restore_backup(main, main_backup)
                print("网络服务重启失败，已恢复原配置。")
            else:
                print(f"配置已写入：{path}" + (f"；备份：{backup}" if backup else ""))
            return result.returncode

        print("没有识别出当前网卡的网络管理组件，未修改，避免误写错误配置。")
        return 1

    def network_manage(self, log: Path) -> int:
        if not self.root_required():
            return 1
        names = self.interfaces()
        if not names:
            print("没有检测到可配置网卡或缺少 ip 命令。")
            return 1
        print("当前网卡：")
        for index, name in enumerate(names, 1):
            print(f"  {index}. {name}")
        raw = input("选择网卡编号：").strip()
        if not raw.isdigit() or not 0 < int(raw) <= len(names):
            return 2
        iface = names[int(raw) - 1]
        backend = self.network_backend(iface)
        current = self.current_network_values(iface)
        print(f"\n检测到网络管理组件：{backend}")
        print(f"当前 IPv4：{current[0] or '未检测到'}")
        print(f"当前网关：{current[1] or '未检测到'}")
        print(f"当前 DNS：{', '.join(current[2]) or '未检测到'}")
        subprocess.run(["ip", "-brief", "address", "show", "dev", iface])
        print("\n1. 查看当前配置\n2. 修改 IPv4、网关和 DNS（自动使用上述组件）")
        choice = ask("选择")
        if choice == "1":
            return 0
        if choice != "2":
            return 2
        values = self.network_prompt(current)
        if values is None:
            return 2
        return self.apply_network_config(iface, backend, *values)

    # ---------- GRUB / IPv6 偏好 ----------

    def grub_manage(self, log: Path) -> int:
        if not self.root_required():
            return 1
        defaults = Path("/etc/default/grub")
        if not defaults.is_file():
            print("没有找到 /etc/default/grub，当前系统可能没有使用 GRUB。")
            return 1
        config_paths = (Path("/boot/grub/grub.cfg"), Path("/boot/grub2/grub.cfg"))
        grub_config = next((path for path in config_paths if path.is_file()), None)
        facts = kernel_manager.collect_kernel_facts()
        config_text = grub_config.read_text(encoding="utf-8", errors="ignore") if grub_config else ""
        entries = kernel_manager.parse_grub_menu_entries(config_text)

        print("== GRUB 引导状态 ==")
        print(f"当前内核：{facts.running_kernel}")
        print(f"/boot 内核映像：{', '.join(facts.boot_images) or '未读取到'}")
        print(f"GRUB 配置：{grub_config or '未找到 grub.cfg，仅允许查看'}")
        print("\n== /etc/default/grub ==")
        print(defaults.read_text(encoding="utf-8", errors="ignore"))
        print("== grubenv ==")
        if shutil.which("grub-editenv"):
            env_result = subprocess.run(["grub-editenv", "list"], text=True, capture_output=True, check=False, timeout=PROBE_TIMEOUT)
            print(env_result.stdout.strip() or env_result.stderr.strip() or "空")
        else:
            print("未安装 grub-editenv")
        print("== 发现的完整启动项 ==")
        for index, entry in enumerate(entries, 1):
            print(f"{index}. {entry}")
        if not entries:
            print("未能从 grub.cfg 解析启动项；不会接受手工猜测的索引或路径。")
        print(
            "\n1. 仅查看状态\n2. 刷新 grub.cfg\n3. 设置永久默认启动项\n"
            "4. 只设置下一次启动项\n5. 修改 timeout\n6. 修改内核参数\n7. 恢复最近备份"
        )
        choice = ask("选择")
        if choice == "1":
            return 0
        if not grub_config:
            print("未找到可生成的 grub.cfg，未做写入。")
            return 1

        def refresh() -> subprocess.CompletedProcess:
            command = ["update-grub"] if shutil.which("update-grub") else ["grub-mkconfig", "-o", str(grub_config)]
            return subprocess.run(command, text=True, check=False)

        if choice == "2":
            return refresh().returncode
        if choice == "7":
            backups = sorted(defaults.parent.glob("grub.yjl-tui.bak.*"), reverse=True)
            if not backups:
                print("没有找到备份。")
                return 1
            shutil.copy2(backups[0], defaults)
            result = refresh()
            if result.returncode:
                print(f"恢复后生成 grub.cfg 失败；已恢复的文件：{backups[0]}")
            else:
                print(f"已恢复并刷新：{backups[0]}")
            return result.returncode
        if choice == "4":
            if not entries or not shutil.which("grub-reboot"):
                print("需要可解析的启动项和 grub-reboot，未设置下一次启动项。")
                return 1
            original = defaults.read_text(encoding="utf-8", errors="ignore")
            if not re.search(r"(?m)^\s*GRUB_DEFAULT=\"?saved\"?\s*$", original):
                print("当前 GRUB_DEFAULT 不是 saved；为避免下次启动失效，请先设置永久默认项为 saved。")
                return 1
            selected = input("输入上方启动项编号：").strip()
            if not selected.isdigit() or not 0 < int(selected) <= len(entries):
                print("启动项编号无效。")
                return 2
            entry = kernel_manager.resolve_grub_entry(entries, entries[int(selected) - 1])
            if not entry:
                print("启动项不在当前 grub.cfg 中。")
                return 1
            result = subprocess.run(["grub-reboot", entry], check=False)
            if result.returncode == 0:
                print(f"已仅设置下一次启动：{entry}\n本工具不会重启；请自行确认维护窗口后重启，并用 uname -r 验证。")
            return result.returncode

        value: str
        key: str
        if choice == "3":
            print("可选 0: GRUB_DEFAULT=saved，配合下一次启动项；或选择一个完整启动项。")
            selected = input("输入 0 或上方启动项编号：").strip()
            if selected == "0":
                value = "saved"
            elif selected.isdigit() and 0 < int(selected) <= len(entries):
                resolved = kernel_manager.resolve_grub_entry(entries, entries[int(selected) - 1])
                if not resolved:
                    print("启动项不在当前 grub.cfg 中。")
                    return 1
                value = resolved
            else:
                print("只允许 0 或已解析的启动项编号。")
                return 2
            key = "GRUB_DEFAULT"
        elif choice == "5":
            value = input("GRUB timeout 秒数 [5]：").strip() or "5"
            if not value.isdigit() or int(value) > 600:
                print("timeout 必须是 0 到 600 的整数。")
                return 2
            key = "GRUB_TIMEOUT"
        elif choice == "6":
            value = input("GRUB_CMDLINE_LINUX_DEFAULT 内容：").strip()
            if any(char in value for char in "\n\r\x00"):
                return 2
            key = "GRUB_CMDLINE_LINUX_DEFAULT"
        else:
            return 2
        backup = defaults.with_name("grub.yjl-tui.bak." + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(defaults, backup)
        original = defaults.read_text(encoding="utf-8", errors="surrogateescape")
        replacement = f'{key}="{value}"'
        pattern = rf"(?m)^\s*#?\s*{re.escape(key)}=.*$"
        updated = re.sub(pattern, replacement, original, count=1)
        if updated == original:
            updated = original.rstrip() + "\n" + replacement + "\n"
        defaults.write_text(updated, encoding="utf-8", errors="surrogateescape")
        result = refresh()
        if result.returncode:
            shutil.copy2(backup, defaults)
            print(f"生成 GRUB 配置失败，已恢复；备份：{backup}")
            return result.returncode
        print(f"已保存：{defaults}\n备份：{backup}\n注意：默认项会在下次启动时生效。")
        return 0

    def ip_preference(self, log: Path) -> int:
        if not self.root_required():
            return 1
        path = Path("/etc/gai.conf")
        print("1. IPv4 优先\n2. IPv6 优先/恢复默认")
        choice = ask("选择")
        if choice not in {"1", "2"}:
            return 2
        backup = self.backup_file(path)
        original = path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
        lines = [line for line in original.splitlines() if not line.startswith("# Managed by yjl-tui") and not line.startswith("precedence ::ffff:0:0/96")]
        if choice == "1":
            lines.extend(["# Managed by yjl-tui", "precedence ::ffff:0:0/96  100"])
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", errors="surrogateescape")
        print(f"已更新地址选择策略：{path}" + (f"；备份：{backup}" if backup else ""))
        print("这只调整地址选择优先级，不会关闭 IPv4/IPv6。可用 curl -4/-6 分别验证实际出站。")
        return 0

    # ---------- WebDAV ----------

    def webdav_manage(self, log: Path) -> int:
        print("== WebDAV 检测 ==")
        for service in ("apache2", "httpd", "nginx", "rclone"):
            if shutil.which(service):
                print(f"命令：{service} 已存在")
        for path in (Path("/etc/apache2"), Path("/etc/httpd"), Path("/etc/nginx")):
            if path.is_dir():
                print(f"配置目录：{path}")
        if shutil.which("ss"):
            subprocess.run(["ss", "-lntup"])
        print("\n1. 重新扫描本机信息\n2. Debian/Ubuntu 安装 Apache WebDAV\n3. 查看 Apache WebDAV 相关配置")
        choice = ask("选择")
        if choice == "1":
            return 0
        if choice == "2":
            if not self.root_required():
                return 1
            profile = system_profile()
            if profile["包管理器"] != "apt":
                print("当前安装器只对 Debian/Ubuntu 做了保守适配；Alpine 请先确认 Apache 模块包名。")
                return 1
            result = subprocess.run(["apt-get", "install", "-y", "apache2", "apache2-utils"])
            if result.returncode:
                return result.returncode
            subprocess.run(["a2enmod", "dav", "dav_fs", "auth_basic"], check=False)
            data = Path("/var/lib/yjl-webdav")
            data.mkdir(parents=True, exist_ok=True)
            user = input("WebDAV 用户名 [yjl]：").strip() or "yjl"
            password = getpass.getpass("WebDAV 密码：")
            if not password:
                print("密码为空，已停止。")
                return 2
            htpasswd = Path("/etc/apache2/.yjl-webdav.htpasswd")
            if not shutil.which("htpasswd"):
                print("缺少 htpasswd，未写入 WebDAV 配置。")
                return 1
            password_result = subprocess.run(["htpasswd", "-i", "-c", str(htpasswd), user], input=password + "\n", text=True, capture_output=True)
            if password_result.returncode:
                print(password_result.stderr.strip() or "生成 WebDAV 密码文件失败。")
                return password_result.returncode
            conf = Path("/etc/apache2/conf-available/yjl-webdav.conf")
            conf.write_text(
                "Alias /webdav /var/lib/yjl-webdav\n<Directory /var/lib/yjl-webdav>\n    DAV On\n    AuthType Basic\n    AuthName \"YJL WebDAV\"\n    AuthUserFile /etc/apache2/.yjl-webdav.htpasswd\n    Require valid-user\n</Directory>\n",
                encoding="utf-8",
            )
            subprocess.run(["a2enconf", "yjl-webdav"], check=False)
            test = subprocess.run(["apache2ctl", "configtest"], check=False)
            if test.returncode:
                print("apache2ctl configtest 未通过，未 reload；请检查 WebDAV 配置。")
                return test.returncode
            result = subprocess.run(["systemctl", "reload", "apache2"])
            print("WebDAV 地址：当前主机的 http(s)://域名/webdav/")
            return result.returncode
        if choice == "3":
            for path in (Path("/etc/apache2"), Path("/etc/httpd"), Path("/etc/nginx")):
                if path.is_dir():
                    subprocess.run(["bash", "-c", f"grep -RniE 'dav|webdav' {shlex.quote(str(path))} 2>/dev/null | sed -n '1,80p'"], check=False)
            return 0
        return 2

    # ---------- TCP 状态与本地方案 ----------

    def tcp_status(self, log: Path) -> int:
        print("== TCP / 内核状态 ==")
        for command in (
            ("uname", "-r"),
            ("sysctl", "-n", "net.ipv4.tcp_congestion_control"),
            ("sysctl", "-n", "net.core.default_qdisc"),
            ("sysctl", "-n", "net.ipv4.tcp_fastopen"),
            ("sysctl", "-n", "net.ipv4.tcp_ecn"),
            ("sysctl", "-n", "net.ipv6.conf.all.disable_ipv6"),
        ):
            if shutil.which(command[0]):
                result = subprocess.run(list(command), text=True, capture_output=True, timeout=PROBE_TIMEOUT)
                print(f"{' '.join(command)}: {result.stdout.strip() or result.stderr.strip()}")
        for name in ("tcp_available_congestion_control", "tcp_allowed_congestion_control"):
            available = Path("/proc/sys/net/ipv4") / name
            if available.is_file():
                print(f"{name}：{available.read_text(encoding='utf-8', errors='ignore').strip()}")
        if shutil.which("lsmod"):
            result = subprocess.run(["lsmod"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
            bbr_modules = [line for line in result.stdout.splitlines() if "bbr" in line.lower()]
            print("BBR 模块：" + ("\n".join(bbr_modules) if bbr_modules else "未列出（多数较新内核将 BBR 内建；以可用算法中含 bbr 为准）"))
        if shutil.which("tc"):
            print("\n== qdisc ==")
            subprocess.run(["tc", "qdisc", "show"])
        return 0

    @staticmethod
    def tcp_profile_data() -> dict:
        try:
            with TCP_PROFILES.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读取 TCP 配置方案失败：{exc}")
            return {}
        if not isinstance(data, dict):
            print("TCP 配置方案格式错误：根节点必须是对象。")
            return {}
        return data

    @staticmethod
    def tcp_parse_settings(path: Path) -> dict[str, str]:
        settings: dict[str, str] = {}
        if not path.is_file():
            return settings
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if re.fullmatch(r"[A-Za-z0-9_.]+", key) and value:
                settings[key] = value
        return settings

    def tcp_dynamic_settings(self, settings: dict[str, str]) -> dict[str, str] | None:
        memory_match = re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")) if Path("/proc/meminfo").is_file() else None
        default_memory = max(1, int(memory_match.group(1)) // 1024) if memory_match else 1024
        prompts = (
            ("网络延迟(ms)", "100"),
            ("本地带宽(Mbps)", "1000"),
            ("VPS带宽(Mbps)", "1000"),
            ("VPS内存(MB)", str(default_memory)),
        )
        values: list[int] = []
        print("激进方案会按 BDP 计算缓冲区。直接回车使用默认值，不会自动重启。")
        for label, default in prompts:
            raw = input(f"{label} [{default}]: ").strip() or default
            if not raw.isdigit() or int(raw) <= 0:
                print(f"{label} 必须是正整数，已取消。")
                return None
            values.append(int(raw))
        latency, local_bw, vps_bw, vps_mem = values
        min_bw = min(local_bw, vps_bw)
        bdp = min_bw * 1_000_000 * latency // 8 // 1000
        max_mem_bytes = vps_mem * 1024 * 1024 * 50 // 100
        rmem_max = min(max(bdp * 2, 1_048_576), max_mem_bytes)
        wmem_max = min(max(bdp * 3 // 2, 1_048_576), max_mem_bytes)
        netdev_backlog = min(max(min_bw * 10, 1000), 10000)
        somaxconn = min(max(vps_mem * 20, 512), 16384)
        syn_backlog = min(somaxconn * 4, 65536)
        init_cwnd = min(max(latency // 20 + 10, 10), 32)
        min_free = min(max(vps_mem * 1024 * 12 // 100, 65536), 524288)
        settings.update({
            "vm.min_free_kbytes": str(min_free),
            "net.core.netdev_max_backlog": str(netdev_backlog),
            "net.core.rmem_max": str(rmem_max),
            "net.core.wmem_max": str(wmem_max),
            "net.core.somaxconn": str(somaxconn),
            "net.ipv4.tcp_rmem": f"32768 262144 {rmem_max}",
            "net.ipv4.tcp_wmem": f"32768 262144 {wmem_max}",
            "net.ipv4.tcp_init_cwnd": str(init_cwnd),
            "net.ipv4.tcp_max_syn_backlog": str(syn_backlog),
        })
        print(f"最终参数：延迟={latency}ms，本地带宽={local_bw}Mbps，VPS带宽={vps_bw}Mbps，内存={vps_mem}MB")
        return settings

    def tcp_apply_profile(self, profile_id: str, log: Path) -> int:
        if not self.root_required():
            return 1
        profiles = self.tcp_profile_data()
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict) or not isinstance(profile.get("settings"), dict):
            print(f"没有找到 TCP 配置方案：{profile_id}")
            return 1
        settings = self.tcp_parse_settings(Path("/etc/sysctl.d/99-yjl-tcp-tuning.conf"))
        for key, value in profile["settings"].items():
            if not re.fullmatch(r"[A-Za-z0-9_.]+", str(key)) or not re.fullmatch(r"[^#\n]+", str(value).strip()):
                print(f"配置方案包含不合法参数：{key}={value}")
                return 2
            settings[str(key)] = str(value).strip()
        if profile.get("dynamic") and self.tcp_dynamic_settings(settings) is None:
            return 2
        if profile.get("settings", {}).get("net.ipv4.tcp_congestion_control"):
            requested = str(profile["settings"]["net.ipv4.tcp_congestion_control"])
            allowed_path = Path("/proc/sys/net/ipv4/tcp_allowed_congestion_control")
            allowed = allowed_path.read_text(encoding="utf-8", errors="ignore").split() if allowed_path.is_file() else []
            if allowed and requested not in allowed:
                print(f"当前内核不支持 {requested}，可用：{' '.join(allowed)}")
                print("请先使用带 (fsc) 的内核安装入口，或选择当前内核支持的方案。")
                return 2
        target = Path("/etc/sysctl.d/99-yjl-tcp-tuning.conf")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = self.backup_file(target)
        lines = [f"{key} = {value}" for key, value in settings.items()]
        target.write_text(
            "# Managed by dandan-tui; profile: " + profile_id + "\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        print(f"已写入：{target}" + (f"；备份：{backup}" if backup else ""))
        if profile.get("limits"):
            limits = Path("/etc/security/limits.d/99-yjl-tui-tcp.conf")
            limits.parent.mkdir(parents=True, exist_ok=True)
            limits_backup = self.backup_file(limits)
            limits.write_text(
                "# Managed by dandan-tui\n* soft nofile 1000000\n* hard nofile 1000000\n",
                encoding="utf-8",
            )
            print(f"已写入：{limits}" + (f"；备份：{limits_backup}" if limits_backup else ""))
            if system_profile()["初始化"] == "systemd":
                dropin = Path("/etc/systemd/system.conf.d/99-yjl-tui-tcp.conf")
                dropin.parent.mkdir(parents=True, exist_ok=True)
                dropin_backup = self.backup_file(dropin)
                dropin.write_text(
                    "# Managed by dandan-tui\n[Manager]\nDefaultLimitNOFILE=1000000\n",
                    encoding="utf-8",
                )
                print(f"已写入：{dropin}" + (f"；备份：{dropin_backup}" if dropin_backup else ""))
                if shutil.which("systemctl"):
                    subprocess.run(["systemctl", "daemon-reload"], check=False)
        if not shutil.which("sysctl"):
            print("缺少 sysctl，参数已保存但没有应用。")
            return 1
        result = subprocess.run(["sysctl", "--system"])
        print("\n应用后的关键状态：")
        self.tcp_status(log)
        if result.returncode:
            print("部分参数可能不受当前内核支持；已保留配置文件，请按输出修正或改用兼容方案。")
        else:
            print(f"本地方案已应用：{profile.get('title', profile_id)}；无需联网安装。")
        return result.returncode

    @staticmethod
    def restore_tcp_file(path: Path) -> None:
        backups = sorted(path.parent.glob(path.name + ".yjl-tui.bak.*"))
        if backups:
            shutil.copy2(backups[0], path)
            print(f"已恢复原文件：{path} <- {backups[0]}")
        elif path.exists():
            path.unlink()
            print(f"已删除 TUI 文件：{path}")

    def tcp_remove_all(self, log: Path) -> int:
        if not self.root_required():
            return 1
        paths = (
            Path("/etc/sysctl.d/99-yjl-tcp-tuning.conf"),
            Path("/etc/security/limits.d/99-yjl-tui-tcp.conf"),
            Path("/etc/systemd/system.conf.d/99-yjl-tui-tcp.conf"),
        )
        for path in paths:
            self.restore_tcp_file(path)
        if shutil.which("systemctl") and Path("/run/systemd/system").exists():
            subprocess.run(["systemctl", "daemon-reload"], check=False)
        if shutil.which("sysctl"):
            result = subprocess.run(["sysctl", "--system"])
        else:
            result = subprocess.CompletedProcess([], 1)
        print("已卸载本 TUI 写入的 TCP 加速配置；没有删除其他软件的 sysctl 文件。")
        return result.returncode

    # ---------- 脚本执行原语 ----------

    def confirm(self, action: dict) -> bool:
        prompt = CONFIRM_PROMPTS.get(action.get("id", ""))
        if not prompt:
            return True
        print(prompt)
        return input("输入 yes 继续，其他任意输入取消：").strip().lower() in {"y", "yes"}

    def interactive(
        self,
        cmd: list[str],
        log: Path,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        pause_after: bool = True,
        display_cmd: str | None = None,
    ) -> int:
        print(f"\n命令：{display_cmd or shlex.join(cmd)}\n日志：{log}")
        print("以下进入原脚本的真实终端交互，结束后按 Enter 返回。\n")
        merged = os.environ.copy()
        if env:
            merged.update(env)
        run_cmd = ["script", "-qefc", shlex.join(cmd), str(log)] if shutil.which("script") else cmd
        try:
            result = subprocess.run(run_cmd, env=merged, cwd=str(cwd) if cwd else None)
        except KeyboardInterrupt:
            return 130
        if pause_after:
            pause()
        return result.returncode

    def download(self, action: dict, log: Path) -> Path | None:
        url = action["url"]
        CACHE.mkdir(parents=True, exist_ok=True)
        target = CACHE / f"{safe_name(action['id'])}.sh"
        temp = CACHE / f".{target.name}.{os.getpid()}.tmp"
        if not shutil.which("curl"):
            print("缺少 curl，不能下载在线脚本。")
            return None
        result = self.run(["curl", "-fL", "--retry", "3", "--connect-timeout", "20", "--max-time", "1800", url, "-o", str(temp)], True, timeout=1900)
        if result.returncode:
            print(result.stderr.strip() or "下载失败")
            temp.unlink(missing_ok=True)
            return None
        temp.chmod(0o700)
        temp.replace(target)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"source: {url}\ncache: {target}\n")
            f.write(result.stderr)
        return target

    @staticmethod
    def syntax_ok(path: Path, interpreter: str) -> bool:
        if interpreter in {"python", "python3"}:
            checker = shutil.which(interpreter)
            if not checker:
                print(f"缺少 {interpreter}，已停止执行。")
                return False
            check_code = (
                "import ast, pathlib, sys; "
                "ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), filename=sys.argv[1])"
            )
            result = subprocess.run([checker, "-c", check_code, str(path)], text=True, capture_output=True)
        else:
            checker = "sh" if interpreter == "sh" else "bash"
            result = subprocess.run([checker, "-n", str(path)], text=True, capture_output=True)
        if result.returncode:
            print("脚本语法检查失败，已停止执行：")
            print(result.stderr.strip())
            return False
        return True

    def run_fnm(self, installer: Path, log: Path) -> int:
        fnm_dir = Path.home() / ".local/share/fnm"
        fnm = fnm_dir / "fnm"
        node_version = "22.11.0"
        command = (
            f"bash {shlex.quote(str(installer))} --skip-shell --force-install --install-dir {shlex.quote(str(fnm_dir))}"
            f" && export PATH={shlex.quote(str(fnm_dir))}:$PATH"
            f" && eval \"$({shlex.quote(str(fnm))} env --shell bash)\""
            f" && {shlex.quote(str(fnm))} install {node_version}"
            f" && {shlex.quote(str(fnm))} default {node_version}"
            f" && {shlex.quote(str(fnm))} use {node_version} && node -v && npm -v"
        )
        result = self.interactive(["bash", "-c", command], log)
        # 只有安装成功后才写 ~/.bashrc，避免链路中途失败留下指向不存在 fnm 的配置块。
        if result == 0:
            bashrc = Path.home() / ".bashrc"
            block = "\n# >>> yjl-tui fnm >>>\nFNM_PATH=" + shlex.quote(str(fnm_dir)) + "\nif [ -d \"$FNM_PATH\" ]; then\n  export PATH=\"$FNM_PATH:$PATH\"\n  eval \"$(fnm env --shell bash)\"\nfi\n# <<< yjl-tui fnm <<<\n"
            old = bashrc.read_text(encoding="utf-8", errors="surrogateescape") if bashrc.exists() else ""
            if "yjl-tui fnm" not in old:
                bashrc.parent.mkdir(parents=True, exist_ok=True)
                with bashrc.open("a", encoding="utf-8") as f:
                    f.write(block)
        return result

    @staticmethod
    def prompt_script_args(prompt: str) -> list[str]:
        """让合集里的参数型脚本（如 DD 重装）能从菜单传参。

        shlex 解析以支持引号包裹的密码等；解析失败或留空时返回空列表——
        对 DD 类脚本这意味着只显示用法，是最安全的回退。
        """
        raw = input(f"{prompt}：").strip()
        if not raw:
            print("未输入参数，按脚本默认行为执行。")
            return []
        try:
            args = shlex.split(raw)
        except ValueError as exc:
            print(f"参数解析失败（{exc}），将不带参数执行（通常会显示用法）。")
            return []
        print(f"附加参数：{shlex.join(args)}")
        return args

    def online(self, action: dict, log: Path) -> int:
        path = self.download(action, log)
        if not path:
            return 1
        interpreter = action.get("interpreter", "bash")
        if not self.syntax_ok(path, interpreter):
            return 2
        if action.get("mode") == "fnm":
            return self.run_fnm(path, log)
        if action.get("mode") == "dd":
            return self.dd_reinstall(action, log, path)
        args = list(map(str, action.get("args", [])))
        prompt = action.get("prompt_args")
        if prompt:
            args.extend(self.prompt_script_args(prompt))
        env = action.get("env") if isinstance(action.get("env"), dict) else None
        return self.interactive([interpreter, str(path), *args], log, env)

    # ---------- DD 重装向导 ----------

    DD_LEITBOGIORO_SYSTEMS = {
        "1": ("debian", "Debian", "12"),
        "2": ("ubuntu", "Ubuntu", "22.04"),
        "3": ("windows", "Windows", "10"),
        "4": ("centos", "CentOS", "9"),
        "5": ("rockylinux", "RockyLinux", "9"),
        "6": ("almalinux", "AlmaLinux", "9"),
    }

    DD_MOECLUB_SYSTEMS = {
        "1": ("d", "Debian", "11"),
        "2": ("u", "Ubuntu", "20.04"),
        "3": ("c", "CentOS", "7"),
    }

    @classmethod
    def build_dd_args(
        cls,
        variant: str,
        system_flag: str,
        version: str,
        password: str,
        port: str,
        firmware: bool = False,
        lang: str = "cn",
    ) -> list[str]:
        """按脚本变体拼装 DD 参数；不合法输入抛 ValueError。"""
        if not version:
            raise ValueError("未提供目标系统版本。")
        if not system_flag:
            raise ValueError("未提供目标系统。")
        if not password and not (variant == "leitbogioro" and system_flag == "windows"):
            # 仅 leitbogioro 的 Windows 允许留空密码（镜像默认 Administrator / Teddysun.com）。
            raise ValueError("未提供新密码。")
        if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
            raise ValueError("SSH 端口必须是 1-65535 的整数。")
        if variant == "leitbogioro":
            args = [f"-{system_flag}", version]
            if system_flag == "windows":
                args += ["-lang", lang]
            if password:
                args += ["-pwd", password]
            if port:
                args += ["-port", port]
            if firmware:
                args.append("-firmware")
            return args
        if variant == "moeclub":
            if system_flag == "windows":
                raise ValueError("MoeClub 入口不提供 Windows；Windows 请使用 leitbogioro 入口。")
            args = [f"-{system_flag}", version, "-v", "64", "-p", password, "-a"]
            if port:
                args += ["-port", port]
            if firmware:
                args.append("-firmware")
            return args
        raise ValueError(f"未知 DD 脚本变体：{variant}")

    def dd_reinstall(self, action: dict, log: Path, script: Path) -> int:
        variant = action.get("dd_variant", "leitbogioro")
        systems = self.DD_LEITBOGIORO_SYSTEMS if variant == "leitbogioro" else self.DD_MOECLUB_SYSTEMS
        print("== DD 重装系统 ==")
        print("警告：DD 会清空硬盘并重装整个操作系统，盘上数据全部丢失；")
        print("重装完成后需用新密码（和端口）重新 SSH 登录，过程通常 10-30 分钟。")
        if input("确认继续？输入 yes：").strip().lower() != "yes":
            print("已取消。")
            return 2
        print("目标系统：")
        for key, (_, label, _) in systems.items():
            print(f"  {key}. {label}")
        print(f"  7. 自定义（手动输入该脚本的完整参数）")
        choice = input("选择 [1]: ").strip() or "1"
        if choice == "7":
            raw = input("输入完整参数：").strip()
            try:
                args = shlex.split(raw)
            except ValueError as exc:
                print(f"参数解析失败（{exc}），已取消。")
                return 2
            if not args:
                print("未输入参数，已取消。")
                return 2
        else:
            if choice not in systems:
                print("无效选择，已取消。")
                return 2
            system_flag, label, default_version = systems[choice]
            version = input(f"{label} 版本 [{default_version}]：").strip() or default_version
            password = getpass.getpass("新密码（重装后的登录密码，不回显）：")
            if variant == "leitbogioro" and system_flag == "windows" and not password:
                print("未设密码，Windows 将使用镜像默认：Administrator / Teddysun.com。")
            elif not password:
                print("密码不能为空，已取消。")
                return 2
            port = input("SSH 端口（留空保持脚本默认）：").strip()
            firmware = input("旧机器需要 -firmware 固件支持？[y/N] ").strip().lower() in {"y", "yes"}
            lang = "cn"
            if variant == "leitbogioro" and system_flag == "windows":
                lang = input("Windows 语言 [cn]：").strip() or "cn"
            try:
                args = self.build_dd_args(variant, system_flag, version, password, port, firmware, lang)
            except ValueError as exc:
                print(f"参数不合法：{exc}")
                return 2
        print("\n将执行：")
        display = shlex.join(["bash", str(script), *args])
        if "-pwd" in args:
            display = re.sub(r"(-pwd\s+)\S+", r"\1******", display)
        print("  " + display)
        if input("最终确认：输入 DD 开始重装（其他任意输入取消）：").strip() != "DD":
            print("已取消。")
            return 2
        print("开始 DD。期间 SSH 会断开；完成后请用新密码重新登录并用 uname -a 核对系统。")
        return self.interactive(["bash", str(script), *args], log, pause_after=False,
                                display_cmd="bash " + shlex.quote(str(script)) + " <DD 参数已隐藏>")

    def local_script(self, action: dict, log: Path) -> int:
        relative = action.get("path")
        if not isinstance(relative, str) or not relative:
            print("本地脚本路径未配置。")
            return 2
        path = (APP_DIR / relative).resolve()
        try:
            path.relative_to(APP_DIR.resolve())
        except ValueError:
            print("本地脚本路径超出 TUI 工作目录，已停止执行。")
            return 2
        if not path.is_file():
            print(f"未找到本地脚本：{path}")
            return 1
        interpreter = action.get("interpreter", "bash")
        if not self.syntax_ok(path, interpreter):
            return 2
        return self.interactive([interpreter, str(path), *map(str, action.get("args", []))], log)

    # ---------- TCP Brutal（实现见 yjl_tui.tcp_brutal，这里保留兼容入口） ----------

    def tcp_brutal_repair(self, log: Path) -> int:
        return tcp_brutal.repair(log)

    @staticmethod
    def _tcp_brutal_patch_jsonc_inbound(text: str) -> tuple[str, bool]:
        return tcp_brutal.patch_jsonc_inbound(text)

    @staticmethod
    def _tcp_brutal_patch_singbox_subscription(text: str, target_tags: set[str]) -> tuple[str, bool]:
        return tcp_brutal.patch_singbox_subscription(text, target_tags)

    @staticmethod
    def _tcp_brutal_patch_yaml_subscription(text: str, target_tags: set[str]) -> tuple[str, bool]:
        return tcp_brutal.patch_yaml_subscription(text, target_tags)

    # ---------- 在线 TCP 脚本 ----------

    def tcp_online(self, action: dict, log: Path) -> int:
        path = self.download(action, log)
        if not path:
            return 1
        interpreter = action.get("interpreter", "bash")
        if not self.syntax_ok(path, interpreter):
            return 2
        choice = str(action.get("tcp_choice", "")).strip()
        if not choice:
            return self.interactive([interpreter, str(path)], log)
        source = path.read_text(encoding="utf-8", errors="surrogateescape")
        marker = '  read -p " 请输入数字 :" num'
        replacement = (
            '  if [[ -n "${YJL_TUI_TCP_CHOICE:-}" ]]; then\n'
            '    num="${YJL_TUI_TCP_CHOICE}"\n'
            '    unset YJL_TUI_TCP_CHOICE\n'
            '  else\n'
            '    read -p " 请输入数字 :" num\n'
            '  fi'
        )
        if marker not in source:
            print("上游 tcp.sh 菜单格式已变化，无法自动定位编号；将打开原始菜单。")
            return self.interactive([interpreter, str(path)], log)
        selected = path.with_name(f".{path.name}.{safe_name(choice)}.selected")
        selected.write_text(source.replace(marker, replacement, 1), encoding="utf-8", errors="surrogateescape")
        try:
            if not self.syntax_ok(selected, interpreter):
                return 2
            return self.interactive([interpreter, str(selected)], log, {"YJL_TUI_TCP_CHOICE": choice})
        finally:
            selected.unlink(missing_ok=True)

    # ---------- 内置动作命令 ----------

    @staticmethod
    def builtin(action_id: str) -> list[str] | None:
        if action_id == "domain_latency":
            hosts = " ".join(shlex.quote(host) for host in DOMAIN_LATENCY_HOSTS)
            script = f'''set +e
for tool in timeout openssl date; do
  command -v "$tool" >/dev/null 2>&1 || {{ echo "缺少命令: $tool"; exit 1; }}
done
# Do not use date +%s%3N here: some Linux date implementations treat %3N
# as full nanoseconds, producing a 19-digit value and Bash arithmetic overflow.
now_ms() {{
  local stamp sec ns
  stamp=$(date +%s%N 2>/dev/null)
  if [[ "$stamp" =~ ^[0-9]{{19}}$ ]]; then
    printf '%s\\n' "$(printf '%s' "$stamp" | cut -c1-13)"
    return 0
  fi
  sec=$(date +%s)
  ns=$(date +%N 2>/dev/null)
  if [[ "$ns" =~ ^[0-9]{{9}}$ ]]; then
    printf '%s%03d\\n' "$sec" "$((10#$ns / 1000000))"
  else
    printf '%s000\\n' "$sec"
  fi
}}
for d in {hosts}; do
  t1=$(now_ms)
  if timeout 1 openssl s_client -connect "$d:443" -servername "$d" </dev/null >/dev/null 2>&1; then
    t2=$(now_ms)
    echo "$d: $((t2 - t1)) ms"
  else
    echo "$d: timeout"
  fi
done'''
            return ["bash", "-c", script]
        commands = {
            "system_info": ["bash", "-c", "echo '== OS =='; cat /etc/os-release 2>/dev/null || true; echo; echo '== Kernel =='; uname -a; echo; echo '== CPU =='; nproc 2>/dev/null || true; lscpu 2>/dev/null | sed -n '1,18p' || true; echo; echo '== Memory =='; free -h 2>/dev/null || true; echo; echo '== Disk =='; df -hT 2>/dev/null || true; echo; echo '== Virtualization =='; systemd-detect-virt 2>/dev/null || true"],
            "port_audit": ["bash", "-c", "echo '== Listening =='; ss -lntup 2>/dev/null || true; echo; echo '== SSH =='; (sshd -T 2>/dev/null || true) | grep -E '^(port|passwordauthentication|permitrootlogin) ' || true; echo; echo '== Services =='; systemctl --type=service --state=running --no-legend 2>/dev/null | sed -n '1,80p' || true"],
            "swap_check": ["bash", "-c", "swapon --show 2>/dev/null || true; echo; cat /proc/swaps 2>/dev/null || true; echo; free -h 2>/dev/null | sed -n '1,3p' || true"],
            "recent_logs": ["bash", "-c", f"find {shlex.quote(str(LOGS))} -type f -printf '%TY-%Tm-%Td %TH:%TM  %p\\n' 2>/dev/null | sort -r | sed -n '1,100p'"],
            "apt_upgrade": ["bash", "-c", "export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get upgrade -y"],
            "swap_builtin": ["bash", "-c", "set -Eeuo pipefail; if swapon --show --noheadings 2>/dev/null | grep -q .; then echo '[SKIP] 已存在活动 Swap，不修改。'; exit 0; fi; if [ ! -f /swapfile ]; then fallocate -l 512M /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=512 status=progress; fi; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile; grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; swapon --show"],
        }
        return commands.get(action_id)

    # ---------- Docker ----------

    @staticmethod
    def docker_binary() -> list[str] | None:
        if shutil.which("docker"):
            return ["docker"]
        print("未找到 docker 命令，请先在“一键在线安装”中安装 Docker。")
        return None

    @staticmethod
    def compose_binary() -> list[str] | None:
        if shutil.which("docker"):
            result = subprocess.run(["docker", "compose", "version"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
            if result.returncode == 0:
                return ["docker", "compose"]
        if shutil.which("docker-compose"):
            return ["docker-compose"]
        print("未找到 Docker Compose 插件或 docker-compose。")
        return None

    @staticmethod
    def _echo_output(result: subprocess.CompletedProcess) -> None:
        for text in (result.stdout, result.stderr):
            if text:
                print(text, end="" if text.endswith("\n") else "\n")

    @classmethod
    def _run_captured(cls, command: list[str], cwd: Path | None, timeout: int, label: str) -> int:
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                cwd=str(cwd) if cwd else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"{label}命令超时。")
            return 124
        cls._echo_output(result)
        return result.returncode

    def docker_run(self, args: list[str], cwd: Path | None = None, timeout: int = 300) -> int:
        binary = self.docker_binary()
        if not binary:
            return 127
        return self._run_captured(binary + args, cwd, timeout, "Docker ")

    @staticmethod
    def compose_run(command: list[str], cwd: Path, timeout: int = 300) -> int:
        return TUI._run_captured(command, cwd, timeout, "Compose ")

    @staticmethod
    def docker_name(value: str, image: bool = False) -> bool:
        pattern = r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*" if image else r"[A-Za-z0-9][A-Za-z0-9_.-]*"
        return bool(re.fullmatch(pattern, value))

    def docker_status(self, log: Path) -> int:
        if not self.docker_binary():
            return 127
        print("== Docker 版本 ==")
        rc = self.docker_run(["version"], timeout=30)
        print("\n== Docker 信息 ==")
        info_rc = self.docker_run(["info"], timeout=30)
        compose = self.compose_binary()
        if compose:
            print("\n== Compose 版本 ==")
            compose_result = subprocess.run(compose + ["version"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
            print(compose_result.stdout or compose_result.stderr, end="")
        if shutil.which("systemctl"):
            print("\n== Docker systemd 状态 ==")
            subprocess.run(["systemctl", "is-active", "docker"], check=False)
        return rc or info_rc

    def docker_containers(self, log: Path) -> int:
        print("== 全部容器 ==")
        return self.docker_run([
            "ps", "-a", "--format",
            "table {{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}",
        ])

    def docker_images(self, log: Path) -> int:
        print("== 本地镜像 ==")
        return self.docker_run([
            "image", "ls", "--format",
            "table {{.Repository}}\\t{{.Tag}}\\t{{.ID}}\\t{{.CreatedSince}}\\t{{.Size}}",
        ])

    def docker_logs(self, log: Path) -> int:
        name = input("容器名：").strip()
        tail = input("显示最近多少行 [200]：").strip() or "200"
        if not self.docker_name(name) or not tail.isdigit() or int(tail) <= 0:
            print("容器名或日志行数格式不合法。")
            return 2
        command = ["logs", "--tail", tail, "--timestamps"]
        if input("是否持续跟踪日志？[y/N] ").strip().lower() in {"y", "yes"}:
            command.append("--follow")
            command.append(name)
            binary = self.docker_binary()
            if not binary:
                return 127
            return self.interactive(binary + command, log, pause_after=False)
        command.append(name)
        return self.docker_run(command)

    def docker_container_action(self, action_id: str, log: Path) -> int:
        name = input("容器名：").strip()
        if not self.docker_name(name):
            print("容器名格式不合法。")
            return 2
        command = {"docker_start": "start", "docker_stop": "stop", "docker_restart": "restart"}[action_id]
        return self.docker_run([command, name])

    def docker_exec(self, log: Path) -> int:
        name = input("容器名：").strip()
        shell = input("容器内 Shell [/bin/sh]：").strip() or "/bin/sh"
        if not self.docker_name(name) or not re.fullmatch(r"/[A-Za-z0-9._/-]+", shell):
            print("容器名或 Shell 路径格式不合法。")
            return 2
        binary = self.docker_binary()
        if not binary:
            return 127
        return self.interactive(binary + ["exec", "-it", name, shell], log, pause_after=False)

    def docker_pull(self, log: Path) -> int:
        image = input("镜像名（例如 nginx:latest）：").strip()
        if not self.docker_name(image, image=True):
            print("镜像名格式不合法。")
            return 2
        return self.docker_run(["pull", image], timeout=1800)

    def docker_remove(self, log: Path) -> int:
        kind = input("1. 删除容器  2. 删除镜像\n选择 [1]: ").strip() or "1"
        if kind not in {"1", "2"}:
            return 2
        name = input("名称：").strip()
        if not self.docker_name(name, image=kind == "2"):
            print("名称格式不合法。")
            return 2
        if kind == "1":
            force = input("容器正在运行时是否强制删除？[y/N] ").strip().lower() in {"y", "yes"}
            args = ["rm"] + (["--force"] if force else []) + [name]
        else:
            args = ["rmi", name]
        return self.docker_run(args)

    def docker_compose(self, log: Path) -> int:
        compose = self.compose_binary()
        if not compose:
            return 127
        raw_path = input("Compose 项目目录 [当前目录]：").strip() or "."
        project = Path(raw_path).expanduser()
        try:
            project = project.resolve()
        except OSError as exc:
            print(f"项目路径无效：{exc}")
            return 2
        if not project.is_dir():
            print(f"目录不存在：{project}")
            return 2
        files = [name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml") if (project / name).is_file()]
        if not files:
            print("目录中没有 compose.yaml、compose.yml 或 docker-compose.yml。")
            return 2
        print(f"已发现：{', '.join(files)}")
        print("1. up -d\n2. down\n3. restart\n4. ps\n5. logs\n6. pull\n7. config 校验")
        choice = ask("选择")
        commands = {
            "1": ["up", "-d"],
            "2": ["down"],
            "3": ["restart"],
            "4": ["ps"],
            "5": ["logs", "--tail", "200"],
            "6": ["pull"],
            "7": ["config"],
        }
        if choice not in commands:
            return 2
        command = compose + commands[choice]
        if choice in {"5"}:
            return self.interactive(command, log, cwd=project, pause_after=False)
        return self.compose_run(command, cwd=project, timeout=1800 if choice in {"1", "6"} else 300)

    def docker_prune(self, log: Path) -> int:
        print("将清理停止容器、未使用网络、悬空镜像和构建缓存。")
        args = ["system", "prune", "--all"]
        if input("是否同时删除未使用的数据卷？[y/N] ").strip().lower() in {"y", "yes"}:
            args.append("--volumes")
        args.append("--force")
        return self.docker_run(args, timeout=1800)

    def docker_daemon_restart(self, log: Path) -> int:
        if shutil.which("systemctl"):
            result = subprocess.run(["systemctl", "restart", "docker"], text=True, capture_output=True, timeout=300)
        elif shutil.which("service"):
            result = subprocess.run(["service", "docker", "restart"], text=True, capture_output=True, timeout=300)
        else:
            print("当前系统没有 systemctl 或 service，无法重启 Docker daemon。")
            return 1
        print(result.stdout or result.stderr, end="")
        return result.returncode

    def lazydocker(self, log: Path) -> int:
        if not shutil.which("lazydocker"):
            print("未找到 lazydocker，请先执行本分类的“安装 lazydocker（官方二进制）”。")
            return 1
        return self.interactive(["lazydocker"], log, pause_after=False)

    # ---------- SSH / 自定义脚本 ----------

    @staticmethod
    def listening(port: str) -> bool:
        if not shutil.which("ss"):
            return False
        try:
            result = subprocess.run(["ss", "-ltn"], text=True, capture_output=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return bool(re.search(r"[:.]" + re.escape(port) + r"\s", result.stdout))

    def ssh_config(self, log: Path) -> int:
        if not self.root_required():
            return 1
        user = input("目标用户 [root]: ").strip() or "root"
        port = input("SSH 新端口 [27272]: ").strip() or "27272"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user) or not port.isdigit() or not 1 <= int(port) <= 65535:
            print("用户名或端口格式不合法。")
            return 2
        if self.run(["id", user], True, timeout=30).returncode or not Path("/etc/ssh/sshd_config").is_file():
            print("目标用户不存在或 sshd_config 不存在。")
            return 1
        password = getpass.getpass("输入新密码（不会写入命令行或日志）：")
        if not password or self.listening(port):
            print("密码不能为空，或目标端口已被占用。")
            return 1
        config = Path("/etc/ssh/sshd_config")
        backup = log.with_suffix(".sshd_config.bak")
        shutil.copy2(config, backup)
        original = config.read_text(encoding="utf-8", errors="surrogateescape")
        updated = re.sub(r"(?im)^\s*#?\s*Port\s+\d+\s*$", "", original).rstrip()
        updated += f"\n\n# Managed by yjl-tui\nPort {port}\nPasswordAuthentication yes\n"
        if user == "root":
            updated += "PermitRootLogin yes\n"
        config.write_text(updated, encoding="utf-8", errors="surrogateescape")
        try:
            if shutil.which("sshd") and self.run(["sshd", "-t", "-f", str(config)], True, timeout=60).returncode:
                raise RuntimeError("sshd -t validation failed")
            result = self.run(["chpasswd"], True, f"{user}:{password}\n", timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "chpasswd failed")
            service = "ssh" if self.run(["systemctl", "list-unit-files", "ssh.service"], True, timeout=60).returncode == 0 else "sshd"
            result = self.run(["systemctl", "reload", service], True, timeout=120)
            if result.returncode:
                result = self.run(["systemctl", "restart", service], True, timeout=120)
            if result.returncode or not self.listening(port):
                raise RuntimeError(result.stderr.strip() or f"port {port} is not listening")
        except Exception as exc:
            shutil.copy2(backup, config)
            print(f"失败，已回滚 SSH 配置：{exc}")
            return 1
        print(f"密码已更新，SSH 已切换到 {port}。备份：{backup}")
        return 0

    def custom(self, log: Path) -> int:
        url = input("输入 http(s) URL：").strip()
        if not re.fullmatch(r"https?://[^\s]+", url, re.I):
            print("只允许 http(s) URL。")
            return 2
        return self.online({"id": "custom", "url": url, "interpreter": "bash", "args": []}, log)

    # ---------- 分发与执行 ----------

    def dispatch(self, action: dict, log: Path) -> int:
        """把一个动作分发到对应的实现；只按 kind / handler / tcp_profile / builtin 四类判定。"""
        kind = action.get("kind")
        if kind == "exit":
            self.should_exit = True
            return 0
        if kind == "online":
            return self.online(action, log)
        if kind == "local_script":
            rc = self.local_script(action, log)
            if action["id"] == "tcp_brutal_install" and rc == 0:
                print("\n安装器完成，开始修复已有 sing-box 配置和结构化订阅……")
                repair_rc = self.tcp_brutal_repair(log)
                return repair_rc if repair_rc else rc
            return rc
        if kind == "tcp_online":
            return self.tcp_online(action, log)
        if kind == "custom":
            return self.custom(log)
        handler_name = self.HANDLER_NAMES.get(action["id"])
        if handler_name:
            rc = getattr(self, handler_name)(log)
            pause()
            return rc
        if action.get("tcp_profile"):
            rc = self.tcp_apply_profile(action["tcp_profile"], log)
            pause()
            return rc
        cmd = self.builtin(action["id"])
        if not cmd:
            print(f"尚未实现内置动作：{action['id']}")
            return 1
        return self.interactive(cmd, log)

    def execute(self, action: dict) -> None:
        log = self.log_file(action["id"])
        self.record_usage(action["id"])
        if not self.confirm(action):
            self.status = "已取消"
            return
        if action.get("needs_root") and not IS_ROOT:
            print("此动作需要 root 权限，请使用 sudo 或 root 登录后启动 TUI。")
            self.status = "权限不足，未执行"
            pause()
            return
        print("\n" + "=" * 70 + f"\n开始：{action['title']}\n" + "=" * 70)
        rc = self.dispatch(action, log)
        self.status = f"{action['title']} 结束，退出码 {rc}；日志：{log}"
        self.rebuild_categories()

    # ---------- curses 界面 ----------

    def draw(self, screen) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        screen.addnstr(0, 0, " YJL Linux TUI  ·  脚本终端管理工具 ", width - 1, curses.color_pair(2) | curses.A_BOLD)
        screen.addnstr(1, 0, f"工作区: {WORKSPACE} | 权限: {'root' if IS_ROOT else '普通用户'}", width - 1, curses.color_pair(3))
        left = max(22, min(30, width // 3))
        screen.vline(3, left, curses.ACS_VLINE, max(1, height - 7))
        screen.addnstr(3, 2, "分类", left - 4, curses.A_BOLD)
        for i, cat in enumerate(self.categories):
            attr = curses.color_pair(1) | curses.A_BOLD if i == self.category else 0
            screen.addnstr(5 + i, 2, ("❯ " if i == self.category else "  ") + cat["title"], left - 4, attr)
        actions = self.current_actions()
        visible_capacity = max(1, height - 12)
        start = max(0, min(self.selected, max(0, len(actions) - visible_capacity)))
        screen.addnstr(3, left + 3, "动作（Enter 执行）", width - left - 5, curses.A_BOLD)
        for row, action in enumerate(actions[start:start + visible_capacity]):
            index = start + row
            attr = curses.color_pair(1) | curses.A_BOLD if index == self.selected else 0
            screen.addnstr(5 + row, left + 3, ("❯ " if index == self.selected else "  ") + action["title"], width - left - 5, attr)
        current = actions[self.selected] if actions else {"description": "此分类没有动作"}
        y = max(5, height - 5)
        screen.hline(y - 1, 0, curses.ACS_HLINE, width)
        screen.addnstr(y, 2, "说明：" + current.get("description", ""), width - 4, curses.color_pair(3))
        screen.addnstr(y + 1, 2, self.status, width - 4)
        screen.addnstr(height - 2, 2, "↑↓/jk 选择  ←→/Tab 分类  / 搜索  Enter 执行  q 退出", width - 4, curses.color_pair(3))
        screen.refresh()

    def run_ui(self, screen) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        screen.keypad(True)
        while True:
            self.draw(screen)
            key = screen.getch()
            actions = self.current_actions()
            if key in (ord("q"), ord("Q")):
                return
            if key == ord("/"):
                # 搜索：退出 curses 后用普通 input 输入（与 execute 的暂停输入同一模式），
                # 结果挂到虚拟分类「搜索：<关键字>」下，用方向键选择后回车执行。
                curses.endwin()
                try:
                    query = input("搜索动作（匹配 id/标题/描述，直接回车清除搜索）：").strip()
                except (EOFError, KeyboardInterrupt):
                    query = ""
                self.set_search(query)
                screen.clear()
            elif key in (curses.KEY_LEFT, curses.KEY_BTAB, 9):
                self.category = (self.category - 1) % len(self.categories)
                self.selected = 0
            elif key == curses.KEY_RIGHT:
                self.category = (self.category + 1) % len(self.categories)
                self.selected = 0
            elif key in (curses.KEY_UP, ord("k")) and actions:
                self.selected = (self.selected - 1) % len(actions)
            elif key in (curses.KEY_DOWN, ord("j")) and actions:
                self.selected = (self.selected + 1) % len(actions)
            elif key in (curses.KEY_ENTER, 10, 13) and actions:
                curses.endwin()
                # 异常屏障：任何动作（包括提示输入时的 Ctrl-C）都不允许带走整个 TUI。
                try:
                    self.execute(actions[self.selected])
                except KeyboardInterrupt:
                    print("\n操作已中断，返回菜单。")
                except Exception as exc:
                    print(f"动作执行出错，已返回菜单：{exc}")
                screen.clear()
                if self.should_exit:
                    return


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print("用法：tui.py [--list|--check|--doctor|--version]")
        return 0
    if "--version" in sys.argv:
        print(f"yjl-tui {VERSION}")
        return 0
    if "--doctor" in sys.argv:
        return run_doctor()
    try:
        config = read_config()
    except Exception as exc:
        print(f"读取配置失败：{exc}", file=sys.stderr)
        return 1
    manager = TUI(config)
    if "--list" in sys.argv:
        titles = {category["id"]: category["title"] for category in manager.categories}
        current = None
        for action in manager.actions:
            cid = action.get("category", "")
            if cid != current:
                current = cid
                print(f"\n# {titles.get(cid, cid or '未分类')}")
            print(f"{action['id']:18} {action['title']}")
        return 0
    if "--check" in sys.argv:
        print(f"yjl-tui {VERSION}")
        print(f"配置 OK: {CONFIG}")
        print(f"TCP 方案: {TCP_PROFILES if TCP_PROFILES.is_file() else '未找到'}")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("需要在真实终端运行；非交互检查请使用 --check。", file=sys.stderr)
        return 2
    if curses is None:
        print("当前 Python 缺少 curses 模块；菜单界面只在 POSIX 终端运行。", file=sys.stderr)
        return 2
    terminal_notice()
    try:
        curses.wrapper(manager.run_ui)
    except curses.error as exc:
        print(f"终端界面初始化失败：{exc}\n请确认在真实终端内运行，且 TERM 设置正确、窗口尺寸足够。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
