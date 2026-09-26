#!/usr/bin/env python3
"""Local Nginx inspection and conservative site management for the TUI."""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


def _is_root() -> bool:
    """POSIX root 判定；无 geteuid 的平台（如 Windows）视为非 root。"""
    getter = getattr(os, "geteuid", None)
    return bool(getter and getter() == 0)


@dataclass
class Site:
    config_path: str
    server_name: list[str] = field(default_factory=list)
    listen: list[str] = field(default_factory=list)
    root: str = ""
    proxy_pass: str = ""
    certificate: str = ""
    certificate_key: str = ""
    locations: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def label(self) -> str:
        names = " ".join(self.server_name) or "未命名站点"
        ports = ", ".join(self.listen) or "未声明监听"
        return f"{names} [{ports}]"


def _server_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        depth, end = 0, None
        for index in range(match.end() - 1, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is not None:
            blocks.append((match.start(), text[match.start():end]))
    return blocks


def parse_sites(config_text: str) -> list[Site]:
    markers = [(match.start(), match.group(1)) for match in re.finditer(r"(?m)^# configuration file (.+):\s*$", config_text)]
    sites: list[Site] = []
    for start, raw in _server_blocks(config_text):
        config_path = next((path for position, path in reversed(markers) if position < start), "(nginx -T)")

        def directive_values(pattern: str) -> list[str]:
            match = re.search(pattern, raw, re.M)
            return match.group(1).split() if match else []

        root = re.search(r"^\s*root\s+([^;]+);", raw, re.M)
        proxy = re.search(r"\bproxy_pass\s+([^;]+);", raw, re.M)
        cert = re.search(r"^\s*ssl_certificate\s+([^;]+);", raw, re.M)
        key = re.search(r"^\s*ssl_certificate_key\s+([^;]+);", raw, re.M)
        sites.append(Site(
            config_path=config_path,
            server_name=directive_values(r"^\s*server_name\s+([^;]+);"),
            listen=directive_values(r"^\s*listen\s+([^;]+);"),
            root=root.group(1).strip() if root else "",
            proxy_pass=proxy.group(1).strip() if proxy else "",
            certificate=cert.group(1).strip() if cert else "",
            certificate_key=key.group(1).strip() if key else "",
            locations=re.findall(r"^\s*location\s+([^\{]+)\{", raw, re.M),
            raw=raw,
        ))
    return sites


def parse_listeners(ss_text: str) -> set[str]:
    return set(re.findall(r"(?::|\])([0-9]{1,5})\b", ss_text))


def valid_domain(value: str) -> bool:
    # 逐段校验，拒绝 "a..example.com" 这类连续点或以连字符开头/结尾的标签。
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    return bool(re.fullmatch(rf"(?:\*\.)?{label}(?:\.{label})+", value))


def valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


# 网站目录不允许落在系统目录及其子树内。
PROTECTED_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root",
                   "/run", "/sbin", "/sys", "/usr", "/var/log", "/var/backups", "/var/lib")


def _under_protected(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in PROTECTED_ROOTS)


def valid_web_root(value: str) -> bool:
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        return False
    normalized = value.rstrip("/") or "/"
    if normalized in {"/", "/var", "/var/www"} or _under_protected(normalized):
        return False
    return True


def static_site_config(domain: str, port: str, root: str) -> str:
    return f'''server {{
    listen {port};
    server_name {domain};
    root {root};
    index index.html;
    charset utf-8;

    location / {{
        try_files $uri $uri/ =404;
    }}

    location ~* \\.(css|js|svg|ico)$ {{
        expires 7d;
        add_header Cache-Control "public, immutable";
    }}
}}
'''


class NginxManager:
    def __init__(self):
        self.sites: list[Site] = []
        self.config_text = ""
        self.listeners: set[str] = set()
        self.nginx_test = ""
        self.service = "unknown"

    @staticmethod
    def _command(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)

    def refresh(self) -> bool:
        if not shutil.which("nginx"):
            print("没有检测到 Nginx，未进入管理菜单。")
            return False
        try:
            check = self._command(["nginx", "-t"])
            config = self._command(["nginx", "-T"])
            listeners = self._command(["ss", "-lntup"])
            service = self._command(["systemctl", "is-active", "nginx"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"读取本机 Nginx 状态失败：{exc}")
            return False
        self.nginx_test = (check.stdout + check.stderr).strip()
        self.config_text = config.stdout + config.stderr
        self.listeners = parse_listeners(listeners.stdout)
        self.service = (service.stdout or service.stderr).strip().splitlines()[0] if (service.stdout or service.stderr).strip() else "unknown"
        self.sites = parse_sites(self.config_text)
        if check.returncode:
            print("Nginx 配置当前未通过校验：\n" + self.nginx_test)
        return True

    def overview(self) -> None:
        print("\n" + "=" * 78)
        print("Nginx 管理工具 - 本机实时总览")
        valid = "通过" if "test is successful" in self.nginx_test else "失败"
        ports = ", ".join(sorted(self.listeners, key=lambda item: int(item))) or "未读取到"
        print(f"Nginx 服务：{self.service}    配置校验：{valid}")
        print(f"站点数量：{len(self.sites)}    当前监听端口：{ports}")
        if not self.sites:
            print("未从 nginx -T 解析到 server 块。")
        for index, site in enumerate(self.sites, 1):
            print(f"\n{index}. {site.label}")
            print(f"   配置：{site.config_path}")
            if site.root:
                print(f"   静态目录：{site.root}")
            if site.proxy_pass:
                print(f"   反代上游：{site.proxy_pass}")
            if site.certificate:
                print(f"   证书：{site.certificate}")
        print("=" * 78)

    def _write_new_conf(self, path: Path, content: str) -> bool:
        if not _is_root():
            print("创建站点和证书管理需要 root 权限。")
            return False
        if path.exists():
            print(f"目标配置已存在，拒绝覆盖：{path}")
            return False
        if path.parent != Path("/etc/nginx/conf.d"):
            print("只允许在 /etc/nginx/conf.d 创建独立站点配置。")
            return False
        backup_dir = Path("/var/backups/yjl-tui/nginx")
        backup_dir.mkdir(parents=True, exist_ok=True)
        # This must remain a non-hidden .conf file so /etc/nginx/conf.d/*.conf
        # includes it during nginx -t before it is promoted to the final path.
        temporary = path.with_name(f"yjl-tui-stage-{path.stem}-{int(time.time())}.tmp.conf")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o644)
            checked = self._command(["nginx", "-t"])
            if checked.returncode:
                print("新配置未通过 nginx -t，未写入正式文件：\n" + (checked.stdout + checked.stderr).strip())
                return False
            os.replace(temporary, path)
            rechecked = self._command(["nginx", "-t"])
            reloaded = self._command(["systemctl", "reload", "nginx"]) if rechecked.returncode == 0 else rechecked
            if rechecked.returncode == 0 and reloaded.returncode == 0:
                print(f"站点已创建并 reload：{path}")
                return True
            try:
                path.replace(backup_dir / f"{path.name}.{int(time.time())}.failed")
            except OSError as move_exc:
                print(f"回退失败，请手动检查 {path}：{move_exc}")
                return False
            self._command(["systemctl", "reload", "nginx"])
            print("reload 失败，新配置已移到备份目录，原站点未被覆盖。")
            return False
        except OSError as exc:
            print(f"写入站点配置失败：{exc}")
            return False
        finally:
            temporary.unlink(missing_ok=True)

    def create_proxy(self) -> bool:
        domain = input("域名：").strip()
        port = input("公网监听 TCP 端口（例如 10301）：").strip()
        upstream = input("反代上游（例如 http://127.0.0.1:9000）：").strip()
        if not valid_domain(domain) or not valid_port(port) or port in self.listeners or not re.fullmatch(r"https?://[^\s;{}]+", upstream):
            print("域名、端口或上游格式不合法；端口必须当前未监听。")
            return False
        filename = re.sub(r"[^A-Za-z0-9.-]+", "-", domain.replace("*.", "wildcard-")) + f"-{port}.conf"
        path = Path("/etc/nginx/conf.d") / filename
        content = f'''server {{
    listen {port};
    server_name {domain};

    location / {{
        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
'''
        print("\n将创建以下配置：\n" + content)
        if input("确认创建并 reload Nginx？[y/N] ").strip().lower() not in {"y", "yes"}:
            return False
        return self._write_new_conf(path, content)

    def create_static_site(self) -> bool:
        domain = input("域名：").strip()
        port = input("公网监听 TCP 端口（例如 10301）：").strip()
        default_root = "/var/www/" + re.sub(r"[^A-Za-z0-9.-]+", "-", domain.replace("*.", "wildcard-"))
        root = input(f"网站目录 [{default_root}]：").strip() or default_root
        if not valid_domain(domain) or not valid_port(port) or port in self.listeners or not valid_web_root(root):
            print("域名、端口或网站目录格式不合法；端口必须当前未监听，网站目录必须是安全的绝对路径。")
            return False
        filename = re.sub(r"[^A-Za-z0-9.-]+", "-", domain.replace("*.", "wildcard-")) + f"-{port}.conf"
        path = Path("/etc/nginx/conf.d") / filename
        content = static_site_config(domain, port, root)
        print("\n将创建以下静态站点配置：\n" + content)
        create_directory = input("同时创建网站目录和默认 index.html？[Y/n] ").strip().lower() not in {"n", "no"}
        if input("确认创建并 reload Nginx？[y/N] ").strip().lower() not in {"y", "yes"}:
            return False
        # 先让配置通过 nginx -t 并生效，再准备网站目录；配置是关键路径，目录失败可以补救。
        if not self._write_new_conf(path, content):
            return False
        if create_directory:
            web_root = Path(root)
            try:
                web_root.mkdir(parents=True, exist_ok=True)
                index = web_root / "index.html"
                if not index.exists():
                    index.write_text(
                        "<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>"
                        + domain + "</title></head><body><h1>" + domain + "</h1></body></html>\n",
                        encoding="utf-8",
                    )
                print(f"网站目录已准备：{web_root}")
            except OSError as exc:
                print(f"站点配置已创建，但网站目录创建失败，站点会 404，请手动创建 {root}：{exc}")
                return False
        else:
            print(f"配置已创建；请自行准备网站目录：{root}")
        return True

    def certificate_status(self) -> None:
        paths = sorted({site.certificate for site in self.sites if site.certificate})
        if paths:
            for path in paths:
                print(f"\n== {path} ==")
                result = self._command(["openssl", "x509", "-in", path, "-noout", "-subject", "-issuer", "-dates", "-ext", "subjectAltName"])
                print((result.stdout + result.stderr).strip())
        else:
            print("当前站点没有发现 ssl_certificate。")
        if shutil.which("certbot"):
            result = self._command(["certbot", "certificates"])
            print("\n== Certbot 证书清单 ==\n" + (result.stdout + result.stderr).strip())
        timer = self._command(["systemctl", "is-enabled", "certbot.timer"])
        active = self._command(["systemctl", "is-active", "certbot.timer"])
        print(f"\nCertbot 自动续期：{'已启用' if timer.returncode == 0 else '未启用'} / {'运行中' if active.returncode == 0 else '未运行'}")

    def _ensure_certbot_timer(self) -> bool:
        if not shutil.which("certbot"):
            print("未安装 certbot。请先用系统包管理器安装 certbot 后再申请证书。")
            return False
        result = self._command(["systemctl", "enable", "--now", "certbot.timer"])
        if result.returncode:
            print("无法启用 certbot.timer：\n" + (result.stdout + result.stderr).strip())
            return False
        print("Certbot 自动续期已启用：certbot.timer。")
        return True

    def issue_certificate(self, selected_method: str = "") -> bool:
        if not _is_root():
            print("申请证书需要 root 权限。")
            return False
        domain = input("要申请/续期的域名：").strip()
        email = input("证书通知邮箱：").strip()
        if not valid_domain(domain) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            print("域名或邮箱格式不合法。")
            return False
        method = selected_method or (input("验证方式：1 HTTP-01（需要公网 TCP 80）  2 Cloudflare DNS-01（Token 将安全保存） [1]：").strip() or "1")
        if method not in {"1", "2"} or not self._ensure_certbot_timer():
            return False
        if method == "1":
            result = self._command(["certbot", "--nginx", "--non-interactive", "--agree-tos", "-m", email, "-d", domain], timeout=300)
        else:
            plugin = self._command(["certbot", "plugins"])
            if "dns-cloudflare" not in plugin.stdout:
                print("缺少 certbot Cloudflare DNS 插件；请先安装 python3-certbot-dns-cloudflare。")
                return False
            token = getpass.getpass("Cloudflare API Token（将以 0600 权限保存，用于自动续期）：")
            if not token:
                return False
            credential_dir = Path("/etc/letsencrypt/yjl-tui")
            credential_dir.mkdir(parents=True, exist_ok=True)
            credential = credential_dir / f"cloudflare-{domain}.ini"
            credential.write_text(f"dns_cloudflare_api_token = {token}\n", encoding="utf-8")
            credential.chmod(0o600)
            result = self._command([
                "certbot", "certonly", "--dns-cloudflare", "--dns-cloudflare-credentials", str(credential),
                "--non-interactive", "--agree-tos", "-m", email, "-d", domain,
            ], timeout=300)
        print((result.stdout + result.stderr).strip())
        if result.returncode:
            if method == "2":
                credential.unlink(missing_ok=True)
            return False
        dry_run = self._command(["certbot", "renew", "--dry-run"], timeout=300)
        print("\n== 自动续期演练 ==\n" + (dry_run.stdout + dry_run.stderr).strip())
        return dry_run.returncode == 0

    def renew_dry_run(self) -> bool:
        if not self._ensure_certbot_timer():
            return False
        result = self._command(["certbot", "renew", "--dry-run"], timeout=300)
        print((result.stdout + result.stderr).strip())
        return result.returncode == 0

    def certificate_menu(self) -> None:
        while True:
            print("\n1. 查看证书与自动续期状态\n2. 申请 HTTP-01 证书并启用自动续期\n3. 申请 Cloudflare DNS-01 证书并启用自动续期\n4. 启用自动续期并执行演练\n0. 返回")
            choice = input("选择 [1]：").strip() or "1"
            if choice == "0":
                return
            if choice == "1":
                self.certificate_status()
            elif choice == "2":
                self.issue_certificate("1")
            elif choice == "3":
                self.issue_certificate("2")
            elif choice == "4":
                self.renew_dry_run()
            else:
                print("无效选择。")
            input("\n按 Enter 继续...")

    def run(self) -> int:
        if not self.refresh():
            return 1
        while True:
            self.overview()
            print("\n1. 刷新并重新扫描\n2. 查看站点原始配置\n3. 证书申请与自动续期\n4. 新建反向代理站点\n5. 新建静态站点\n0. 返回上级菜单")
            choice = input("选择 [1]：").strip() or "1"
            if choice == "0":
                return 0
            if choice == "1":
                self.refresh()
            elif choice == "2":
                raw = input("站点编号：").strip()
                if raw.isdigit() and 0 < int(raw) <= len(self.sites):
                    print(self.sites[int(raw) - 1].raw)
                else:
                    print("站点编号无效。")
            elif choice == "3":
                self.certificate_menu()
                self.refresh()
            elif choice == "4":
                self.create_proxy()
                self.refresh()
            elif choice == "5":
                self.create_static_site()
                self.refresh()
            else:
                print("无效选择。")
            input("\n按 Enter 继续...")


def run_nginx_manager() -> int:
    return NginxManager().run()
