"""Pure planning and parsing helpers for system-kernel maintenance."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess


SUPPORTED_RELEASES = {
    ("debian", "11"),
    ("debian", "12"),
    ("debian", "13"),
    ("ubuntu", "22.04"),
    ("ubuntu", "24.04"),
    ("ubuntu", "26.04"),
}
SUPPORTED_ARCHITECTURES = {"amd64", "arm64"}


@dataclass(frozen=True)
class KernelIdentity:
    distro_id: str
    version_id: str
    codename: str
    architecture: str


@dataclass(frozen=True)
class PackagePlan:
    label: str
    packages: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class KernelFacts:
    identity: KernelIdentity
    running_kernel: str
    virtualization: str
    bootloader: str
    boot_images: tuple[str, ...]
    secure_boot: str
    dkms_status: tuple[str, ...]
    boot_free_bytes: int

    def installation_block_reason(self) -> str | None:
        if self.virtualization.lower() in {"docker", "lxc", "lxc-libvirt", "openvz", "container"}:
            return "容器共享宿主机内核，不能在容器内安装或切换内核。"
        if not supported_distribution(self.identity):
            return "当前系统或 CPU 架构暂不在系统内核维护支持范围内。"
        if self.bootloader == "none":
            return "未检测到可管理的本机引导环境，不能安全安装内核。"
        return None


def supported_distribution(identity: KernelIdentity) -> bool:
    return (
        identity.architecture in SUPPORTED_ARCHITECTURES
        and (identity.distro_id, identity.version_id) in SUPPORTED_RELEASES
    )


def _output(command: tuple[str, ...]) -> str:
    if not command or not shutil.which(command[0]):
        return ""
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=60,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return values
    for line in lines:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def collect_kernel_facts() -> KernelFacts:
    os_release = _os_release()
    architecture = _output(("dpkg", "--print-architecture")) or _output(("uname", "-m"))
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    virtualization = _output(("systemd-detect-virt",)) or "none"
    bootloader = "grub" if Path("/etc/default/grub").is_file() else "none"
    secure_boot = _output(("mokutil", "--sb-state")) or "unknown"
    boot_images = tuple(path.name for path in sorted(Path("/boot").glob("vmlinuz-*"))) if Path("/boot").is_dir() else ()
    dkms = tuple(line for line in _output(("dkms", "status")).splitlines() if line)
    try:
        boot_free = shutil.disk_usage("/boot").free
    except OSError:
        boot_free = 0
    return KernelFacts(
        KernelIdentity(os_release.get("ID", ""), os_release.get("VERSION_ID", ""), os_release.get("VERSION_CODENAME", ""), architecture),
        _output(("uname", "-r")) or "未知",
        virtualization,
        bootloader,
        boot_images,
        secure_boot,
        dkms,
        boot_free,
    )


def format_kernel_report(facts: KernelFacts) -> str:
    return "\n".join((
        f"系统：{facts.identity.distro_id} {facts.identity.version_id} ({facts.identity.codename or '未知'})",
        f"架构：{facts.identity.architecture or '未知'}；虚拟化：{facts.virtualization}",
        f"当前内核：{facts.running_kernel}",
        f"引导：{facts.bootloader}；/boot 可用：{facts.boot_free_bytes // 1024 // 1024} MiB",
        f"已安装内核：{', '.join(facts.boot_images) or '未读取到'}",
        f"Secure Boot：{facts.secure_boot}",
        f"DKMS：{'; '.join(facts.dkms_status) or '未检测到'}",
    ))


def recommended_apt_plan(
    identity: KernelIdentity,
    candidates: dict[str, str],
    preferred_track: str = "stable",
) -> PackagePlan | None:
    """Return an install plan only when every package has an APT candidate."""
    if not supported_distribution(identity):
        return None
    packages: tuple[str, ...]
    label: str
    if identity.distro_id == "debian":
        arch = identity.architecture
        packages = (f"linux-image-{arch}", f"linux-headers-{arch}")
        label = (
            f"Debian {identity.version_id} Backports 内核"
            if preferred_track == "backports"
            else f"Debian {identity.version_id} 官方稳定内核"
        )
    elif preferred_track == "hwe":
        version = identity.version_id
        packages = (f"linux-generic-hwe-{version}", f"linux-headers-generic-hwe-{version}")
        label = f"Ubuntu {version} HWE 内核"
    else:
        packages = ("linux-generic", "linux-headers-generic")
        label = f"Ubuntu {identity.version_id} 官方稳定内核"
    if not all(candidates.get(package) for package in packages):
        return None
    source = "debian-backports" if preferred_track == "backports" else "configured-apt"
    return PackagePlan(label, packages, source)


def debian_backports_source(identity: KernelIdentity) -> str | None:
    """Return the TUI-owned Debian backports line for supported Debian only."""
    if identity.distro_id != "debian" or not supported_distribution(identity):
        return None
    if not re.fullmatch(r"[a-z0-9-]+", identity.codename):
        return None
    return f"deb http://deb.debian.org/debian {identity.codename}-backports main"


def backports_apt_arguments(identity: KernelIdentity, packages: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return APT arguments that force the explicitly configured backports suite."""
    if not debian_backports_source(identity) or not packages:
        return None
    return ("apt-get", "install", "-y", "-t", f"{identity.codename}-backports", *packages)


def mainline_package_plan(version: str, page: str, architecture: str) -> PackagePlan | None:
    """Return package names only for complete, successful AMD64 Mainline builds."""
    if architecture != "amd64" or "Test amd64 succeeded" not in page:
        return None
    if "CHECKSUMS" not in page or "CHECKSUMS.gpg" not in page:
        return None
    package_names = tuple(
        match.group(1)
        for match in re.finditer(r"""href=["']([^"']+_(?:all|amd64)\.deb)["']""", page)
    )
    if not package_names:
        package_names = tuple(
            match.group(0)
            for match in re.finditer(
                r"linux-(?:headers|image-unsigned|modules)-[A-Za-z0-9.+-]+_[A-Za-z0-9.+-]+_(?:all|amd64)\.deb",
                page,
            )
        )
    required = ("linux-headers-", "linux-image-unsigned-", "linux-modules-")
    if not all(any(name.startswith(prefix) for name in package_names) for prefix in required):
        return None
    if not any(name.endswith("_all.deb") and name.startswith("linux-headers-") for name in package_names):
        return None
    return PackagePlan(f"Ubuntu Mainline {version}", package_names, "kernel.ubuntu.com")


def mainline_sha256sums(manifest: str, package_names: tuple[str, ...]) -> dict[str, str] | None:
    """Read only the expected SHA-256 records and reject incomplete manifests."""
    sums: dict[str, str] = {}
    wanted = set(package_names)
    for line in manifest.splitlines():
        match = re.fullmatch(r"\s*([0-9A-Fa-f]{64})\s+\*?([^\s]+)\s*", line)
        if match and match.group(2) in wanted:
            sums[match.group(2)] = match.group(1).lower()
    return sums if set(sums) == wanted else None


def parse_grub_menu_entries(config_text: str) -> tuple[str, ...]:
    """Parse GRUB menuentry/submenu braces into full, stable entry paths."""
    entries: list[str] = []
    submenus: list[str] = []
    blocks: list[str] = []
    pending: tuple[str, str] | None = None
    token_re = re.compile(r"(?P<kind>submenu|menuentry)\s+'(?P<title>[^']+)'|(?P<brace>[{}])")
    for match in token_re.finditer(config_text):
        kind = match.group("kind")
        title = match.group("title")
        brace = match.group("brace")
        if kind and title:
            pending = (kind, title)
            if kind == "menuentry":
                entries.append(">".join((*submenus, title)))
            continue
        if brace == "{":
            block_kind = pending[0] if pending else "other"
            blocks.append(block_kind)
            if pending and pending[0] == "submenu":
                submenus.append(pending[1])
            pending = None
        elif brace == "}" and blocks:
            if blocks.pop() == "submenu":
                submenus.pop()
    return tuple(entries)


def resolve_grub_entry(entries: tuple[str, ...], selected: str) -> str | None:
    """Return a complete GRUB entry path only when it was discovered locally."""
    return selected if selected in entries else None
