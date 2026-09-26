"""Read-only system, CPU, and virtualization probes shared by the menu actions."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .paths import STATE, WELCOME_MARKER

VIRTUALIZATION_LABELS = {
    "kvm": "KVM",
    "qemu": "QEMU",
    "vmware": "VMware",
    "microsoft": "Hyper-V",
    "microsoft-hyper-v": "Hyper-V",
    "oracle": "VirtualBox",
    "bhyve": "bhyve",
    "xen": "Xen",
    "lxc": "LXC",
    "lxc-libvirt": "LXC",
    "docker": "Docker",
    "podman": "Podman",
    "openvz": "OpenVZ",
    "systemd-nspawn": "systemd-nspawn",
    "none": "未检测到",
}

PROBE_TIMEOUT = 60


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def command_output(command: list[str]) -> str:
    """Return command output without making optional probes fatal."""
    if not command or not shutil.which(command[0]):
        return ""
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def read_text_value(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    if value.lower() in {"", "none", "unknown", "not specified", "to be filled by o.e.m."}:
        return ""
    return value


def parse_lscpu(output: str) -> dict[str, str]:
    """Parse the stable ``label: value`` form emitted by lscpu."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            values[key] = value
    return values


def read_cpuinfo_records() -> list[dict[str, str]]:
    try:
        source = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    records: list[dict[str, str]] = []
    for block in source.split("\n\n"):
        record: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() and value.strip():
                record[key.strip()] = value.strip()
        if record:
            records.append(record)
    return records


def cpu_hardware_profile() -> dict[str, str]:
    """Collect CPU details visible to the current Linux guest or host."""
    lscpu = parse_lscpu(command_output(["lscpu"]))
    records = read_cpuinfo_records()
    first = records[0] if records else {}

    def value(*keys: str, default: str = "未知") -> str:
        for key in keys:
            candidate = lscpu.get(key) or first.get(key)
            if candidate:
                return candidate
        return default

    models = list(dict.fromkeys(record.get("model name", "") for record in records))
    models = [model for model in models if model]
    flags = value("Flags", "flags", default="")
    if lscpu.get("Virtualization"):
        cpu_virtualization = lscpu["Virtualization"]
    elif "vmx" in flags.split():
        cpu_virtualization = "Intel VT-x (vmx)"
    elif "svm" in flags.split():
        cpu_virtualization = "AMD-V (svm)"
    else:
        cpu_virtualization = "未识别"

    cache_parts = [
        f"{label}: {lscpu[label]}"
        for label in ("L1d cache", "L1i cache", "L2 cache", "L3 cache")
        if lscpu.get(label)
    ]
    cache = "; ".join(cache_parts) or value("cache size")
    frequency = value("CPU MHz", "cpu MHz", default="未提供")
    if frequency not in {"未知", "未提供"} and re.fullmatch(r"[0-9.]+", frequency):
        frequency = f"{frequency} MHz"

    return {
        "逻辑 CPU": value("CPU(s)", default=str(len(records) or "未知")),
        "CPU 架构": value("Architecture", default=command_output(["uname", "-m"]) or "未知"),
        "CPU 厂商": value("Vendor ID", "vendor_id"),
        "CPU 型号": "；".join(models) if models else value("Model name", "model name"),
        "CPU 家族": value("CPU family", "cpu family"),
        "型号编号": value("Model", "model"),
        "步进": value("Stepping", "stepping"),
        "插槽数": value("Socket(s)", default="未知"),
        "每插槽核心数": value("Core(s) per socket", "cpu cores"),
        "每核心线程数": value("Thread(s) per core", default="未知"),
        "当前频率": frequency,
        "缓存": cache,
        "地址宽度": value("Address sizes", default="未知"),
        "CPU 虚拟化支持": cpu_virtualization,
        "虚拟化厂商": value("Hypervisor vendor", default="未检测到"),
        "虚拟化类型": value("Virtualization type", default="未检测到"),
        "NUMA": value("NUMA node(s)", default="未知"),
        "指令集": flags or "未读取到",
    }


def service_active(name: str) -> bool:
    """True when the given systemd unit is currently active."""
    return bool(shutil.which("systemctl") and subprocess.run(
        ["systemctl", "is-active", "--quiet", name], check=False
    ).returncode == 0)


def virtualization_profile(cpu: dict[str, str] | None = None) -> dict[str, str]:
    """Identify VM/container type and expose DMI evidence when available."""
    detected = command_output(["systemd-detect-virt"]) or ""
    vm = command_output(["systemd-detect-virt", "--vm"]) or "none"
    container = command_output(["systemd-detect-virt", "--container"]) or "none"
    detected_label = VIRTUALIZATION_LABELS.get(detected.lower(), detected or "未知")
    vm_label = VIRTUALIZATION_LABELS.get(vm.lower(), vm)
    container_label = VIRTUALIZATION_LABELS.get(container.lower(), container)
    if vm != "none":
        running_mode = "虚拟机"
    elif container != "none":
        running_mode = "容器"
    else:
        running_mode = "物理机或未识别"

    cpu = cpu or {}
    hypervisor = cpu.get("虚拟化厂商", "未检测到")
    if detected in {"", "none"} and hypervisor != "未检测到":
        detected_label = hypervisor
    if running_mode == "虚拟机":
        host_cpu = "虚拟机内不可直接读取；下方为宿主机暴露给本机的 vCPU"
    elif running_mode == "容器":
        host_cpu = "容器与宿主机共享 CPU；下方为当前系统可见 CPU"
    else:
        host_cpu = "当前系统可见 CPU（未确认存在虚拟化层）"

    return {
        "虚拟化环境": detected_label,
        "运行形态": running_mode,
        "虚拟机检测": vm_label,
        "容器检测": container_label,
        "Hypervisor 厂商": hypervisor,
        "DMI 系统厂商": read_text_value("/sys/class/dmi/id/sys_vendor") or "未提供",
        "DMI 产品型号": read_text_value("/sys/class/dmi/id/product_name") or "未提供",
        "DMI 产品版本": read_text_value("/sys/class/dmi/id/product_version") or "未提供",
        "DMI 主板型号": read_text_value("/sys/class/dmi/id/board_name") or "未提供",
        "内核虚拟化标记": "是（hypervisor）" if "hypervisor" in cpu.get("指令集", "").split() else "未发现",
        "宿主机 CPU 读取": host_cpu,
    }


def system_profile() -> dict[str, str]:
    osr = read_os_release()
    if shutil.which("systemctl") and Path("/run/systemd/system").exists():
        init = "systemd"
    elif shutil.which("rc-service"):
        init = "openrc"
    else:
        init = "unknown"
    if shutil.which("apt-get"):
        package_manager = "apt"
    elif shutil.which("apk"):
        package_manager = "apk"
    elif shutil.which("dnf"):
        package_manager = "dnf"
    elif shutil.which("yum"):
        package_manager = "yum"
    else:
        package_manager = "unknown"

    if service_active("NetworkManager.service"):
        network = "NetworkManager"
    elif service_active("systemd-networkd.service"):
        network = "systemd-networkd"
    elif service_active("networking.service"):
        network = "ifupdown"
    elif init == "openrc" and shutil.which("rc-service"):
        network = "OpenRC networking"
    elif Path("/etc/netplan").is_dir():
        network = "netplan（待确认后端）"
    elif Path("/etc/network/interfaces").is_file() or Path("/etc/network/interfaces.d").is_dir():
        network = "ifupdown（服务状态未知）"
    else:
        network = "未识别"
    virt = virtualization_profile()
    return {
        "系统": osr.get("PRETTY_NAME", osr.get("ID", "未知")),
        "系统 ID": osr.get("ID", "未知"),
        "版本": osr.get("VERSION_ID", "未知"),
        "初始化": init,
        "包管理器": package_manager,
        "网络管理": network,
        "架构": subprocess.run(["uname", "-m"], text=True, capture_output=True, timeout=PROBE_TIMEOUT).stdout.strip() or "未知",
        "内核": subprocess.run(["uname", "-r"], text=True, capture_output=True, timeout=PROBE_TIMEOUT).stdout.strip() or "未知",
        "启动模式": "UEFI" if Path("/sys/firmware/efi").exists() else "BIOS/未知",
        "虚拟化": virt["虚拟化环境"],
    }


def terminal_notice() -> None:
    """Show the one-time splash outside curses so it works on tiny terminals too."""
    if WELCOME_MARKER.exists():
        return
    print("\033[36m")
    print("+----------------------------------------------------------+")
    print("|                                                          |")
    print("|              YJL 专用脚本                               |")
    print("|          Linux TUI Server Manager                        |")
    print("|                                                          |")
    print("+----------------------------------------------------------+")
    print("\033[0m", end="", flush=True)
    time.sleep(1)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        WELCOME_MARKER.touch(exist_ok=True)
    except OSError:
        # A read-only host should still be able to use the menu.
        pass
