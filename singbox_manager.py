#!/usr/bin/env python3
"""Interactive maintenance for fscarmen split-config sing-box installations."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit


APP_DIR = Path(__file__).resolve().parent
DEFAULT_WORK_DIR = Path(os.environ.get("YJL_SINGBOX_WORK_DIR", "/etc/sing-box"))
SERVICE_NAME = "sing-box"
MANAGED_ROUTE_FILE = "00_yjl_singbox_routes.json"
MANAGED_DNS_FILE = "99_yjl_singbox_dns.json"
STATE_FILE = "yjl-tui-state.json"
BACKUP_DIR_NAME = "yjl-tui-backups"
TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DNS_STRATEGIES = {
    "1": (None, "沿用 fscarmen 当前 DNS 策略"),
    "2": ("prefer_ipv4", "IPv4 优先"),
    "3": ("prefer_ipv6", "IPv6 优先"),
    "4": ("ipv4_only", "仅 IPv4"),
    "5": ("ipv6_only", "仅 IPv6"),
}
SERVICE_RULESETS = {
    "1": ("OpenAI / ChatGPT", "geosite-openai"),
    "2": ("Claude", "geosite-anthropic"),
    "3": ("Gemini", "geosite-google-gemini"),
    "4": ("Google DeepMind", "geosite-google-deepmind"),
    "5": ("Netflix", "geosite-netflix"),
    "6": ("Disney+", "geosite-disney"),
    "7": ("HBO / Max", "geosite-hbo"),
    "8": ("Prime Video", "geosite-primevideo"),
    "9": ("YouTube", "geosite-youtube"),
    "10": ("Spotify", "geosite-spotify"),
    "11": ("Telegram", "geosite-telegram"),
    "12": ("TikTok", "geosite-tiktok"),
    "13": ("Bilibili", "geosite-bilibili"),
}


class ManagerError(RuntimeError):
    """A recoverable, user-facing manager failure."""


@dataclass(frozen=True)
class SocksProxy:
    tag: str
    server: str
    server_port: int
    username: str
    password: str

    def masked(self) -> str:
        user = f"{self.username[:2]}***@" if self.username else ""
        return f"socks5://{user}{self.server}:{self.server_port}"

    def as_config(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "socks",
            "tag": self.tag,
            "server": self.server,
            "server_port": self.server_port,
            "version": "5",
        }
        if self.username:
            result["username"] = self.username
            result["password"] = self.password
        return result


def now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def ensure_tag(value: str, label: str = "tag") -> str:
    value = value.strip()
    if not TAG_RE.fullmatch(value):
        raise ManagerError(f"{label} 只能包含字母、数字、点、下划线和连字符，长度不超过 64。")
    return value


def ensure_rule_name(value: str) -> str:
    value = value.strip().removesuffix(".srs")
    if not re.fullmatch(r"geosite-[a-z0-9._@-]+", value):
        raise ManagerError("规则集名称必须类似 geosite-openai。")
    return value


def managed_rule_tag(rule_name: str) -> str:
    return ensure_tag(f"yjl-{ensure_rule_name(rule_name)}", "规则 tag")


def parse_socks5_url(value: str, tag: str) -> SocksProxy:
    """Parse a pasteable SOCKS5 URL without writing it to shell history."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ManagerError(f"SOCKS5 地址格式错误：{exc}") from exc
    if parsed.scheme.lower() not in {"socks5", "socks5h", "socks"}:
        raise ManagerError("只支持 socks5://、socks5h:// 或 socks:// 地址。")
    if not parsed.hostname:
        raise ManagerError("SOCKS5 地址缺少主机名或 IP。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManagerError("SOCKS5 端口格式错误。") from exc
    if not port or not 1 <= port <= 65535:
        raise ManagerError("SOCKS5 端口必须在 1 到 65535 之间。")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if bool(username) != bool(password):
        raise ManagerError("认证 SOCKS5 必须同时包含用户名和密码。")
    if len(username.encode()) > 255 or len(password.encode()) > 255:
        raise ManagerError("SOCKS5 用户名或密码过长。")
    return SocksProxy(ensure_tag(tag, "出站 tag"), parsed.hostname, port, username, password)


def empty_state() -> dict[str, Any]:
    return {"version": 1, "socks": [], "routes": {}, "dns_strategy": None}


def normalize_state(data: Any) -> dict[str, Any]:
    state = empty_state()
    if not isinstance(data, dict):
        return state
    socks = data.get("socks")
    if isinstance(socks, list):
        for item in socks:
            if not isinstance(item, dict):
                continue
            try:
                proxy = SocksProxy(
                    ensure_tag(str(item.get("tag", "")), "出站 tag"),
                    str(item.get("server", "")).strip(),
                    int(item.get("server_port", 0)),
                    str(item.get("username", "")),
                    str(item.get("password", "")),
                )
                if proxy.server and 1 <= proxy.server_port <= 65535 and (not proxy.username or proxy.password):
                    state["socks"].append(proxy.__dict__)
            except (ManagerError, TypeError, ValueError):
                continue
    routes = data.get("routes")
    if isinstance(routes, dict):
        for rule, outbound in routes.items():
            try:
                state["routes"][ensure_rule_name(str(rule))] = ensure_tag(str(outbound), "出站 tag")
            except ManagerError:
                continue
    dns = data.get("dns_strategy")
    if dns in {None, "prefer_ipv4", "prefer_ipv6", "ipv4_only", "ipv6_only"}:
        state["dns_strategy"] = dns
    return state


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def strip_jsonc_comments(content: str) -> str:
    """Remove JSONC comments without treating // inside a JSON string as one."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(content):
        char = content[index]
        following = content[index + 1] if index + 1 < len(content) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and following == "/":
            index += 2
            while index < len(content) and content[index] not in "\r\n":
                index += 1
        elif char == "/" and following == "*":
            index += 2
            while index < len(content) - 1 and not (content[index] == "*" and content[index + 1] == "/"):
                if content[index] in "\r\n":
                    output.append(content[index])
                index += 1
            index += 2 if index < len(content) - 1 else 0
        else:
            output.append(char)
            index += 1
    return "".join(output)


def load_state(path: Path) -> dict[str, Any]:
    return normalize_state(load_json(path, empty_state()))


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


# sing-box 配置检查等服务无关命令的统一超时；规则同步走网络，单独放宽。
COMMAND_TIMEOUT = 60
SYNC_TIMEOUT = 1800


def build_managed_route_fragment(state: dict[str, Any], rules_dir: Path) -> dict[str, Any]:
    outbounds = [SocksProxy(**item).as_config() for item in state["socks"]]
    rule_sets: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for source_rule, outbound in sorted(state["routes"].items()):
        rule_sets.append(
            {
                "tag": managed_rule_tag(source_rule),
                "type": "local",
                "format": "binary",
                # 分片配置运行在 Linux；固定 POSIX 分隔符，保证生成的 path 与平台无关。
                "path": (rules_dir / f"{source_rule}.srs").as_posix(),
            }
        )
        # This intentionally precedes fscarmen's 03_route.json and uses its
        # broadly compatible `outbound` route syntax.
        rules.append({"rule_set": [managed_rule_tag(source_rule)], "outbound": outbound})
    result: dict[str, Any] = {}
    if outbounds:
        result["outbounds"] = outbounds
    if outbounds or rule_sets or rules:
        # This fragment sorts before fscarmen's 01_outbounds.json. sing-box
        # otherwise falls back to the first merged outbound, which would turn
        # a newly added SOCKS5 proxy into an unintended global proxy.
        result["route"] = {"final": "direct", "rule_set": rule_sets, "rules": rules}
    return result


def build_dns_fragment(strategy: str | None) -> dict[str, Any] | None:
    return None if strategy is None else {"dns": {"strategy": strategy}}


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def receive_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ManagerError("SOCKS5 服务端提前关闭连接。")
        data.extend(chunk)
    return bytes(data)


def probe_socks5(proxy: SocksProxy, target_host: str = "api.openai.com", target_port: int = 443, timeout: float = 12.0) -> str:
    """Prove TCP connection, optional RFC 1929 auth, and SOCKS CONNECT only."""
    try:
        with socket.create_connection((proxy.server, proxy.server_port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            methods = b"\x02\x00" if proxy.username else b"\x00"
            sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
            version, method = receive_exact(sock, 2)
            if version != 5 or method == 0xFF:
                raise ManagerError("SOCKS5 服务端拒绝认证方式。")
            if method == 0x02:
                user = proxy.username.encode()
                password = proxy.password.encode()
                if not user or not password:
                    raise ManagerError("SOCKS5 服务端要求用户名密码，但当前出站未配置认证。")
                sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
                auth_version, auth_status = receive_exact(sock, 2)
                if auth_version != 1 or auth_status != 0:
                    raise ManagerError("SOCKS5 用户名或密码认证失败。")
            elif method != 0x00:
                raise ManagerError("SOCKS5 服务端返回未知认证方式。")

            host = target_host.encode("idna")
            if len(host) > 255:
                raise ManagerError("测试目标域名过长。")
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + target_port.to_bytes(2, "big"))
            header = receive_exact(sock, 4)
            if header[0] != 5:
                raise ManagerError("SOCKS5 CONNECT 返回了无效版本。")
            if header[1] != 0:
                meanings = {1: "通用失败", 2: "规则拒绝", 3: "网络不可达", 4: "主机不可达", 5: "连接被拒绝", 6: "超时"}
                raise ManagerError(f"SOCKS5 CONNECT {target_host}:{target_port} 失败：{meanings.get(header[1], f'错误码 {header[1]}')}。")
            address_length = 4 if header[3] == 1 else 16 if header[3] == 4 else receive_exact(sock, 1)[0] if header[3] == 3 else 0
            if not address_length:
                raise ManagerError("SOCKS5 CONNECT 返回了未知地址类型。")
            receive_exact(sock, address_length + 2)
    except OSError as exc:
        raise ManagerError(f"无法连接 SOCKS5：{exc}") from exc
    return f"SOCKS5 认证和 CONNECT {target_host}:{target_port} 已通过。"


class SingBoxManager:
    def __init__(self, work_dir: Path = DEFAULT_WORK_DIR, app_dir: Path = APP_DIR) -> None:
        self.work_dir = work_dir
        self.conf_dir = work_dir / "conf"
        self.binary = work_dir / "sing-box"
        self.rules_dir = Path(os.environ.get("YJL_GEOSITE_RULE_DIR", str(work_dir / "rules")))
        self.state_path = work_dir / STATE_FILE
        self.backups_dir = work_dir / BACKUP_DIR_NAME
        self.update_script = app_dir / "scripts/geosite/update.sh"

    @property
    def route_path(self) -> Path:
        return self.conf_dir / MANAGED_ROUTE_FILE

    @property
    def dns_path(self) -> Path:
        return self.conf_dir / MANAGED_DNS_FILE

    def compatible(self) -> tuple[bool, str]:
        if not self.work_dir.is_dir():
            return False, f"未找到 {self.work_dir}。"
        if not self.conf_dir.is_dir() or not list(self.conf_dir.glob("*.json")):
            return False, f"未找到 fscarmen 配置目录 {self.conf_dir}。"
        required = ("01_outbounds.json", "03_route.json", "05_dns.json")
        missing = [name for name in required if not (self.conf_dir / name).is_file()]
        if missing:
            return False, f"未检测到完整 fscarmen 分片配置，缺少：{', '.join(missing)}。"
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            return False, f"未找到可执行 sing-box：{self.binary}。"
        return True, "检测到 fscarmen 分片配置。"

    def check(self, conf_dir: Path | None = None) -> tuple[bool, str]:
        target = conf_dir or self.conf_dir
        result = subprocess.run([str(self.binary), "check", "-C", str(target)], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output or ("配置检查通过。" if result.returncode == 0 else "配置检查失败。")

    def service_kind(self) -> str:
        if shutil.which("rc-service") and Path("/etc/init.d/sing-box").exists():
            return "openrc"
        if shutil.which("systemctl"):
            return "systemd"
        return "unknown"

    def service_status(self) -> str:
        kind = self.service_kind()
        if kind == "openrc":
            result = subprocess.run(["rc-service", SERVICE_NAME, "status"], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        elif kind == "systemd":
            result = subprocess.run(["systemctl", "is-active", SERVICE_NAME], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        else:
            return "未识别 init 系统"
        return (result.stdout + result.stderr).strip() or ("运行中" if result.returncode == 0 else "未运行")

    def running_core_pids(self) -> list[str]:
        """Find actual fscarmen core processes without relying on an OpenRC pidfile."""
        expected = f"{self.binary} run -C {self.conf_dir}"
        result = subprocess.run(["ps", "-o", "pid,args"], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        pids: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].strip() == expected:
                pids.append(parts[0])
        return pids

    def binary_version(self) -> str:
        result = subprocess.run([str(self.binary), "version"], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        return (result.stdout + result.stderr).strip().splitlines()[0] if result.returncode == 0 else "读取失败"

    def current_state(self) -> dict[str, Any]:
        return load_state(self.state_path)

    def available_outbounds(self) -> list[str]:
        tags = {"direct"}
        for path in self.conf_dir.glob("*.json"):
            data = load_json(path, {})
            if not isinstance(data, dict):
                continue
            for key in ("outbounds", "endpoints"):
                values = data.get(key)
                if isinstance(values, list):
                    tags.update(str(item.get("tag")) for item in values if isinstance(item, dict) and item.get("tag"))
        for item in self.current_state()["socks"]:
            tags.add(item["tag"])
        return sorted(tags)

    def configured_inbounds(self) -> list[str]:
        listeners: list[str] = []
        for path in sorted(self.conf_dir.glob("*.json")):
            data = load_json(path, {})
            if not isinstance(data, dict) or not isinstance(data.get("inbounds"), list):
                continue
            for inbound in data["inbounds"]:
                if not isinstance(inbound, dict) or not inbound.get("listen_port"):
                    continue
                tag = str(inbound.get("tag") or inbound.get("type") or "未命名")
                listeners.append(f"{tag}:{inbound['listen_port']}")
        return listeners

    def status_lines(self) -> list[str]:
        ok, message = self.compatible()
        lines = [f"安装：{message}"]
        if not ok:
            return lines
        check_ok, check_message = self.check()
        state = self.current_state()
        lines.extend(
            [
                f"核心：{self.binary_version()}",
                f"服务：{self.service_status()}",
                f"配置检查：{'通过' if check_ok else '失败'} - {check_message.splitlines()[-1]}",
                f"入站端口：{', '.join(self.configured_inbounds()) or '未从配置中读到'}",
                f"出站：{', '.join(self.available_outbounds()) or '无'}",
                f"受管 SOCKS5：{len(state['socks'])} 个；受管分流：{len(state['routes'])} 条",
                f"DNS 覆盖：{state['dns_strategy'] or '未设置'}",
                f"规则目录：{self.rules_dir}（已安装 {len(list(self.rules_dir.glob('*.srs')))} 个）",
                f"备份目录：{self.backups_dir}（{len(list(self.backups_dir.glob('*')))} 个）",
            ]
        )
        return lines

    def create_backup(self, label: str, full: bool = False) -> Path:
        backup = self.backups_dir / f"{now_stamp()}-{label}"
        backup.mkdir(parents=True, mode=0o700)
        managed = backup / "managed"
        managed.mkdir(mode=0o700)
        for path in (self.route_path, self.dns_path, self.state_path):
            marker = managed / f"{path.name}.missing"
            if path.exists():
                shutil.copy2(path, managed / path.name)
            else:
                marker.write_text("missing\n", encoding="ascii")
        if full:
            archive = backup / "config.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(self.conf_dir, arcname="conf", recursive=True)
                if self.state_path.exists():
                    tar.add(self.state_path, arcname=STATE_FILE, recursive=False)
        return backup

    def restore_managed_snapshot(self, backup: Path) -> None:
        managed = backup / "managed"
        if not managed.is_dir():
            raise ManagerError("备份不包含受管配置快照。")
        for target in (self.route_path, self.dns_path, self.state_path):
            source = managed / target.name
            missing = managed / f"{target.name}.missing"
            if source.exists():
                atomic_write(target, source.read_text(encoding="utf-8"), 0o600)
            elif missing.exists():
                target.unlink(missing_ok=True)

    def stage_check(self, state: dict[str, Any]) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="yjl-singbox-stage-") as temp_name:
            staged_conf = Path(temp_name) / "conf"
            shutil.copytree(self.conf_dir, staged_conf, symlinks=True)
            route = build_managed_route_fragment(state, self.rules_dir)
            dns = build_dns_fragment(state["dns_strategy"])
            (staged_conf / MANAGED_ROUTE_FILE).write_text(json_text(route), encoding="utf-8")
            if dns is None:
                (staged_conf / MANAGED_DNS_FILE).unlink(missing_ok=True)
            else:
                (staged_conf / MANAGED_DNS_FILE).write_text(json_text(dns), encoding="utf-8")
            return self.check(staged_conf)

    def sync_rules(self, names: list[str]) -> None:
        names = sorted({ensure_rule_name(item) for item in names})
        if not names:
            return
        if not self.update_script.is_file():
            raise ManagerError(f"未找到 Geosite 更新器：{self.update_script}")
        env = {**os.environ, "YJL_GEOSITE_RULE_DIR": str(self.rules_dir)}
        result = subprocess.run(["bash", str(self.update_script), "--sync", *names], text=True, env=env, check=False, timeout=SYNC_TIMEOUT)
        if result.returncode != 0:
            raise ManagerError("Geosite 规则同步失败。")
        missing = [name for name in names if not (self.rules_dir / f"{name}.srs").is_file()]
        if missing:
            raise ManagerError(f"规则同步后仍缺少：{', '.join(missing)}")

    def reload_service(self) -> tuple[bool, str]:
        if os.environ.get("YJL_SINGBOX_SKIP_RELOAD") == "1":
            return True, "已按 YJL_SINGBOX_SKIP_RELOAD 跳过服务重载"
        kind = self.service_kind()
        if kind == "openrc":
            running = self.running_core_pids()
            if running:
                result = subprocess.run(["kill", "-HUP", *running], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
                time.sleep(1)
                active = self.running_core_pids()
                if result.returncode == 0 and active:
                    return True, f"已向运行中的 sing-box 进程发送 HUP（PID：{', '.join(active)}）"
                return False, "sing-box HUP 热加载失败，进程未保持运行。"

            subprocess.run(["rc-service", SERVICE_NAME, "zap"], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
            result = subprocess.run(["rc-service", SERVICE_NAME, "start"], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
            time.sleep(1)
            active = self.running_core_pids()
            if result.returncode == 0 and active:
                return True, f"已通过 OpenRC 启动 sing-box（PID：{', '.join(active)}）"
            return False, (result.stdout + result.stderr).strip() or "OpenRC 未启动 sing-box 进程。"
        elif kind == "systemd":
            result = subprocess.run(["systemctl", "reload", SERVICE_NAME], text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT)
        else:
            return False, "未识别 init 系统，未重载服务。"
        active = self.service_status()
        return result.returncode == 0 and ("started" in active.lower() or "active" in active.lower() or "运行" in active), active

    def apply_state(self, state: dict[str, Any]) -> str:
        ok, message = self.compatible()
        if not ok:
            raise ManagerError(message)
        state = normalize_state(state)
        for route, outbound in state["routes"].items():
            if outbound not in self.available_outbounds() and outbound not in {item["tag"] for item in state["socks"]}:
                raise ManagerError(f"规则 {route} 指向不存在的出站：{outbound}")
        self.sync_rules(list(state["routes"]))
        ok, output = self.stage_check(state)
        if not ok:
            raise ManagerError(f"暂存配置未通过 sing-box check：{output}")

        backup = self.create_backup("before-apply")
        route = build_managed_route_fragment(state, self.rules_dir)
        dns = build_dns_fragment(state["dns_strategy"])
        atomic_write(self.route_path, json_text(route))
        if dns is None:
            self.dns_path.unlink(missing_ok=True)
        else:
            atomic_write(self.dns_path, json_text(dns))
        atomic_write(self.state_path, json_text(state))
        ok, output = self.check()
        if ok:
            service_ok, service_text = self.reload_service()
            if service_ok:
                return f"配置已应用；备份：{backup}；服务：{service_text}"
            output = f"服务重载失败：{service_text}"

        self.restore_managed_snapshot(backup)
        self.check()
        self.reload_service()
        raise ManagerError(f"应用失败，已恢复 {backup}：{output}")

    def restore_full_backup(self, backup: Path) -> str:
        archive = backup / "config.tar.gz"
        if not archive.is_file():
            raise ManagerError("选择的备份不是完整配置备份。")
        with tempfile.TemporaryDirectory(prefix="yjl-singbox-restore-", dir=self.work_dir) as temp_name:
            temp = Path(temp_name)
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
                    raise ManagerError("备份包含不安全路径，拒绝还原。")
                allowed = [member for member in members if member.name == STATE_FILE or member.name == "conf" or member.name.startswith("conf/")]
                for member in allowed:
                    if member.issym() or member.islnk() or not member.isfile() and not member.isdir():
                        raise ManagerError("备份包含不安全文件类型，拒绝还原。")
                    tar.extract(member, temp)
            staged = temp / "conf"
            if not staged.is_dir():
                raise ManagerError("备份中没有 conf 目录。")
            ok, output = self.check(staged)
            if not ok:
                raise ManagerError(f"备份配置未通过 sing-box check：{output}")
            before = self.create_backup("before-restore", full=True)
            old_conf = self.work_dir / f"conf.before-restore-{now_stamp()}"
            os.replace(self.conf_dir, old_conf)
            try:
                os.replace(staged, self.conf_dir)
            except OSError:
                # 第二步失败时必须把原配置放回去，否则 conf_dir 会消失。
                os.replace(old_conf, self.conf_dir)
                raise
            staged_state = temp / STATE_FILE
            if staged_state.exists():
                atomic_write(self.state_path, staged_state.read_text(encoding="utf-8"))
            else:
                self.state_path.unlink(missing_ok=True)
            ok, output = self.check()
            service_ok, service_text = self.reload_service() if ok else (False, output)
            if ok and service_ok:
                shutil.rmtree(old_conf)
                return f"已还原 {backup.name}；还原前备份：{before}；服务：{service_text}"
            shutil.rmtree(self.conf_dir)
            os.replace(old_conf, self.conf_dir)
            self.restore_managed_snapshot(before)
            self.check()
            self.reload_service()
            raise ManagerError(f"还原未生效，已恢复还原前配置：{service_text}")

    def add_proxy(self, state: dict[str, Any], proxy: SocksProxy) -> dict[str, Any]:
        state = normalize_state(state)
        existing = [item for item in state["socks"] if item["tag"] != proxy.tag]
        existing.append(proxy.__dict__)
        state["socks"] = sorted(existing, key=lambda item: item["tag"])
        return state

    def interactive(self) -> int:
        ok, message = self.compatible()
        print("\n".join(self.status_lines()))
        if not ok:
            return 2
        while True:
            print("\n--- sing-box管理 ---")
            print("1. 刷新状态")
            print("2. Geosite 规则更新")
            print("3. SOCKS5 出站管理")
            print("4. 服务分流管理")
            print("5. DNS IPv4 / IPv6 优先级")
            print("6. 配置备份与还原")
            print("7. 重新应用当前受管配置")
            print("0. 返回")
            choice = input("选择：").strip()
            try:
                if choice == "0":
                    return 0
                if choice == "1":
                    print("\n".join(self.status_lines()))
                elif choice == "2":
                    self.sync_rules([value[1] for value in SERVICE_RULESETS.values()])
                    print("Geosite 常用规则已同步。")
                elif choice == "3":
                    self.menu_proxies()
                elif choice == "4":
                    self.menu_routes()
                elif choice == "5":
                    self.menu_dns()
                elif choice == "6":
                    self.menu_backups()
                elif choice == "7":
                    print(self.apply_state(self.current_state()))
                else:
                    print("无效选择。")
            except ManagerError as exc:
                print(f"失败：{exc}")
            except (OSError, subprocess.SubprocessError) as exc:
                # 磁盘/命令层面的意外错误也不应带走整个管理菜单。
                print(f"系统错误：{exc}")
            input("按 Enter 继续...")

    def menu_proxies(self) -> None:
        state = self.current_state()
        print("\n已有 SOCKS5 出站：")
        for item in state["socks"]:
            print(f"- {SocksProxy(**item).tag}: {SocksProxy(**item).masked()}")
        print("1. 粘贴 SOCKS5 URL 新增/覆盖")
        print("2. 分字段新增/覆盖")
        print("3. 测试已有 SOCKS5")
        print("4. 删除 SOCKS5")
        choice = input("选择：").strip()
        if choice == "1":
            tag = input("出站 tag（例如 ovh-openai）：").strip()
            proxy = parse_socks5_url(getpass.getpass("SOCKS5 URL（输入不回显）：").strip(), tag)
            self.apply_state(self.add_proxy(state, proxy))
            print(f"已保存 {proxy.tag}。")
        elif choice == "2":
            tag = ensure_tag(input("出站 tag："), "出站 tag")
            host = input("主机/IP：").strip()
            try:
                port = int(input("端口：").strip())
            except ValueError as exc:
                raise ManagerError("端口必须是数字。") from exc
            username = input("用户名（无认证直接留空）：")
            password = getpass.getpass("密码（无认证直接留空，输入不回显）：")
            proxy = SocksProxy(tag, host, port, username, password)
            if not host or not 1 <= port <= 65535 or bool(username) != bool(password):
                raise ManagerError("SOCKS5 字段无效。")
            self.apply_state(self.add_proxy(state, proxy))
            print(f"已保存 {proxy.tag}。")
        elif choice == "3":
            proxy = self.select_proxy(state)
            print(probe_socks5(proxy))
        elif choice == "4":
            tag = input("删除的出站 tag：").strip()
            if tag in state["routes"].values():
                raise ManagerError("该出站仍被分流规则使用，请先修改或删除对应规则。")
            state["socks"] = [item for item in state["socks"] if item["tag"] != tag]
            self.apply_state(state)
            print(f"已删除 {tag}。")

    def select_proxy(self, state: dict[str, Any]) -> SocksProxy:
        proxies = [SocksProxy(**item) for item in state["socks"]]
        if not proxies:
            raise ManagerError("尚未配置受管 SOCKS5 出站。")
        for index, proxy in enumerate(proxies, 1):
            print(f"{index}. {proxy.tag}  {proxy.masked()}")
        try:
            return proxies[int(input("选择 SOCKS5：").strip()) - 1]
        except (ValueError, IndexError) as exc:
            raise ManagerError("无效 SOCKS5 选择。") from exc

    def menu_routes(self) -> None:
        state = self.current_state()
        print("\n服务规则：")
        for key, (title, rule) in SERVICE_RULESETS.items():
            print(f"{key}. {title} ({rule}) -> {state['routes'].get(rule, '未设置')}")
        print("A. 设置服务出站  C. 独立 Geosite 规则集分流  D. 删除服务规则")
        action = input("选择：").strip().lower()
        if action == "a":
            service = SERVICE_RULESETS.get(input("服务编号：").strip())
            if not service:
                raise ManagerError("无效服务编号。")
            outbounds = self.available_outbounds()
            for index, outbound in enumerate(outbounds, 1):
                print(f"{index}. {outbound}")
            try:
                target = outbounds[int(input("目标出站：").strip()) - 1]
            except (ValueError, IndexError) as exc:
                raise ManagerError("无效出站选择。") from exc
            state["routes"][service[1]] = target
            print(self.apply_state(state))
        elif action == "c":
            rule = ensure_rule_name(input("规则集（例如 geosite-github）："))
            outbounds = self.available_outbounds()
            for index, outbound in enumerate(outbounds, 1):
                print(f"{index}. {outbound}")
            try:
                target = outbounds[int(input("目标出站：").strip()) - 1]
            except (ValueError, IndexError) as exc:
                raise ManagerError("无效出站选择。") from exc
            state["routes"][rule] = target
            print(self.apply_state(state))
        elif action == "d":
            service = SERVICE_RULESETS.get(input("服务编号：").strip())
            if not service:
                raise ManagerError("无效服务编号。")
            state["routes"].pop(service[1], None)
            print(self.apply_state(state))

    def menu_dns(self) -> None:
        state = self.current_state()
        for key, (_, title) in DNS_STRATEGIES.items():
            print(f"{key}. {title}")
        selected = DNS_STRATEGIES.get(input("选择：").strip())
        if not selected:
            raise ManagerError("无效 DNS 选择。")
        state["dns_strategy"] = selected[0]
        print(self.apply_state(state))

    def menu_backups(self) -> None:
        print("1. 创建完整配置备份")
        print("2. 还原完整配置备份")
        choice = input("选择：").strip()
        if choice == "1":
            print(f"已创建备份：{self.create_backup('manual', full=True)}")
        elif choice == "2":
            backups = sorted(path for path in self.backups_dir.glob("*") if (path / "config.tar.gz").is_file())
            if not backups:
                raise ManagerError("没有可还原的完整配置备份。")
            for index, backup in enumerate(backups, 1):
                print(f"{index}. {backup.name}")
            try:
                print(self.restore_full_backup(backups[int(input("选择备份：").strip()) - 1]))
            except (ValueError, IndexError) as exc:
                raise ManagerError("无效备份选择。") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="fscarmen sing-box maintenance manager")
    parser.add_argument("--status", action="store_true", help="只输出本机 sing-box 状态")
    parser.add_argument("--probe-socks5", metavar="URL", help="只测试 SOCKS5；传 - 时从标准输入读取 URL")
    args = parser.parse_args()
    manager = SingBoxManager()
    if args.probe_socks5 is not None:
        value = sys.stdin.readline().strip() if args.probe_socks5 == "-" else args.probe_socks5
        try:
            print(probe_socks5(parse_socks5_url(value, "yjl-probe")))
            return 0
        except ManagerError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 1
    if args.status:
        print("\n".join(manager.status_lines()))
        return 0 if manager.compatible()[0] else 2
    return manager.interactive()


if __name__ == "__main__":
    raise SystemExit(main())
