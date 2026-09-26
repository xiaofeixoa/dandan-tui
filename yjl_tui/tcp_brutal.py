"""TCP Brutal 修补：sing-box 入站配置与结构化订阅的纯文本处理。

不依赖 TUI 状态，也不执行命令（除 ``repair`` 的检查/重载外）；全部函数可独立测试。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def matching_brace(text: str, start: int) -> int | None:
    """Return the index of the ``}`` closing the ``{`` at ``start``, skipping strings/comments."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
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
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def patch_multiplex_block(block: str) -> tuple[str, bool]:
    updated = block
    changed = False
    brutal_match = re.search(r'(?m)^\s*"brutal"\s*:\s*\{', updated)
    direct_region_end = brutal_match.start() if brutal_match else len(updated)
    direct_region = updated[:direct_region_end]
    direct_enabled = re.search(r'("enabled"\s*:\s*)(?:true|false)', direct_region)
    if direct_enabled and direct_enabled.group(0).endswith("false"):
        start, end = direct_enabled.span()
        direct_region = direct_region[:start] + direct_enabled.group(1) + "true" + direct_region[end:]
        updated = direct_region + updated[direct_region_end:]
        changed = True

    brutal_match = re.search(r'(?m)\s*"brutal"\s*:\s*\{', updated)
    if brutal_match:
        brutal_start = updated.find("{", brutal_match.start())
        brutal_end = matching_brace(updated, brutal_start)
        if brutal_end is not None:
            brutal_block = updated[brutal_start:brutal_end + 1]
            brutal_enabled = re.search(r'("enabled"\s*:\s*)(?:true|false)', brutal_block)
            if brutal_enabled:
                if brutal_enabled.group(0).endswith("false"):
                    start, end = brutal_enabled.span()
                    brutal_block = brutal_block[:start] + brutal_enabled.group(1) + "true" + brutal_block[end:]
                    changed = True
            else:
                brutal_block = brutal_block[:-1] + '\n                    "enabled": true\n                }'
                changed = True
            updated = updated[:brutal_start] + brutal_block + updated[brutal_end + 1:]
    else:
        closing = updated.rfind("}")
        if closing > 0:
            prefix = updated[:closing].rstrip()
            separator = "" if prefix.endswith(("{", ",")) else ","
            addition = (
                f'{separator}\n                "brutal": {{\n'
                '                    "enabled": true,\n'
                '                    "up_mbps": 1000,\n'
                '                    "down_mbps": 1000\n'
                '                }\n            '
            )
            updated = prefix + addition + updated[closing:]
            changed = True
    return updated, changed


def patch_jsonc_inbound(text: str) -> tuple[str, bool]:
    pattern = re.compile(r'"multiplex"\s*:\s*\{')
    cursor = 0
    pieces: list[str] = []
    changed = False
    found = False
    while True:
        match = pattern.search(text, cursor)
        if not match:
            pieces.append(text[cursor:])
            break
        opening = text.find("{", match.start())
        closing = matching_brace(text, opening)
        if closing is None:
            pieces.append(text[cursor:])
            break
        found = True
        pieces.append(text[cursor:opening])
        block, block_changed = patch_multiplex_block(text[opening:closing + 1])
        pieces.append(block)
        changed = changed or block_changed
        cursor = closing + 1
    updated = "".join(pieces)
    if found:
        return updated, changed

    inbounds = re.search(r'"inbounds"\s*:\s*\[', updated)
    if not inbounds:
        return text, False
    opening = updated.find("{", inbounds.end())
    closing = matching_brace(updated, opening)
    if opening < 0 or closing is None:
        return text, False
    prefix = updated[:closing].rstrip()
    separator = "" if prefix.endswith(("{", ",")) else ","
    addition = (
        f'{separator}\n            "multiplex": {{\n'
        '                "enabled": true,\n'
        '                "padding": true,\n'
        '                "brutal": {\n'
        '                    "enabled": true,\n'
        '                    "up_mbps": 1000,\n'
        '                    "down_mbps": 1000\n'
        '                }\n'
        '            }\n        '
    )
    return prefix + addition + updated[closing:], True


def supported_node(node: dict) -> bool:
    node_type = str(node.get("type", "")).lower()
    if node_type in {"shadowtls", "shadowsocks", "trojan"}:
        return True
    if node_type == "vmess":
        network = node.get("network") or (node.get("transport") or {}).get("type")
        return str(network).lower() == "ws"
    if node_type == "vless":
        if str(node.get("flow", "")).lower() == "xtls-rprx-vision":
            return False
        network = node.get("network") or (node.get("transport") or {}).get("type")
        return str(network).lower() in {"ws", "http", "grpc"}
    return False


def patch_singbox_subscription(text: str, target_tags: set[str]) -> tuple[str, bool]:
    source = re.sub(r'(?m)^\s*//.*\n', "", text)
    source = re.sub(r",\s*([}\]])", r"\1", source)
    try:
        data = json.loads(source)
    except json.JSONDecodeError:
        return text, False
    changed = False

    def visit(value):
        nonlocal changed
        if isinstance(value, dict):
            tag = str(value.get("tag", ""))
            if tag in target_tags and supported_node(value):
                multiplex = value.setdefault("multiplex", {})
                if not isinstance(multiplex, dict):
                    multiplex = {}
                    value["multiplex"] = multiplex
                if multiplex.get("enabled") is not True:
                    multiplex["enabled"] = True
                    changed = True
                brutal = multiplex.setdefault("brutal", {})
                if not isinstance(brutal, dict):
                    brutal = {}
                    multiplex["brutal"] = brutal
                if brutal.get("enabled") is not True:
                    brutal["enabled"] = True
                    changed = True
                brutal.setdefault("up_mbps", 1000)
                brutal.setdefault("down_mbps", 1000)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    if not changed:
        return text, False
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n", True


def yaml_supported(block: str, target_tags: set[str]) -> bool:
    if not any(tag and tag in block for tag in target_tags):
        return False
    node_type = re.search(r'\btype:\s*([A-Za-z0-9_-]+)', block, re.I)
    if not node_type:
        return False
    node_type = node_type.group(1).lower()
    if node_type in {"shadowtls", "ss", "trojan"}:
        return True
    network = re.search(r'\bnetwork:\s*([A-Za-z0-9_-]+)', block, re.I)
    flow = re.search(r'\bflow:\s*([^,\s}]+)', block, re.I)
    if node_type == "vmess":
        return bool(network and network.group(1).lower() == "ws")
    if node_type == "vless":
        return bool(
            network
            and network.group(1).lower() in {"ws", "http", "grpc"}
            and not (flow and flow.group(1).lower() == "xtls-rprx-vision")
        )
    return False


def yaml_set_nested_enabled(block: str, key: str) -> tuple[str, bool]:
    inline = re.compile(rf'({re.escape(key)}\s*:\s*\{{\s*enabled\s*:\s*)(false|true)', re.I)
    updated, count = inline.subn(r"\1true", block, count=1)
    changed = count > 0 and updated != block
    lines = updated.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = re.match(rf'^(\s*){re.escape(key)}\s*:\s*$', line, re.I)
        if not match:
            continue
        key_indent = len(match.group(1).replace("\t", "    "))
        for child in range(index + 1, len(lines)):
            stripped = lines[child].strip()
            if not stripped:
                continue
            child_indent = len(lines[child]) - len(lines[child].lstrip(" \t"))
            if child_indent <= key_indent:
                break
            enabled = re.match(r'^(\s*enabled\s*:\s*)(false|true)(\s*)$', lines[child], re.I)
            if enabled and enabled.group(2).lower() == "false":
                lines[child] = enabled.group(1) + "true" + enabled.group(3) + ("\n" if lines[child].endswith("\n") else "")
                changed = True
                break
    return "".join(lines), changed


def patch_yaml_block(block: str) -> tuple[str, bool]:
    updated, smux_changed = yaml_set_nested_enabled(block, "smux")
    updated, brutal_changed = yaml_set_nested_enabled(updated, "brutal-opts")
    changed = smux_changed or brutal_changed
    has_smux = re.search(r'\bsmux\s*:', updated, re.I) is not None
    has_brutal = re.search(r'\bbrutal-opts\s*:', updated, re.I) is not None
    if has_smux and not has_brutal:
        if "\n" not in updated:
            closing = updated.rfind("}")
            if closing > 0:
                updated = (
                    updated[:closing].rstrip()
                    + ", brutal-opts: { enabled: true, up: 1000 Mbps, down: 1000 Mbps }"
                    + updated[closing:]
                )
                changed = True
        else:
            newline = "\r\n" if "\r\n" in updated else "\n"
            base = re.search(r'(?m)^(\s*)type:', updated)
            indent = base.group(1) if base else "  "
            core = updated.rstrip("\r\n")
            updated = core + newline + (
                f"{indent}brutal-opts:{newline}"
                f"{indent}  enabled: true{newline}"
                f"{indent}  up: 1000 Mbps{newline}"
                f"{indent}  down: 1000 Mbps{newline}"
            )
            changed = True
    elif not has_smux:
        newline = "\r\n" if "\r\n" in updated else "\n"
        base = re.search(r'(?m)^(\s*)type:', updated)
        indent = base.group(1) if base else "  "
        if "\n" not in updated:
            closing = updated.rfind("}")
            if closing > 0:
                updated = (
                    updated[:closing].rstrip()
                    + ", smux: { enabled: true, protocol: h2mux, padding: true },"
                    + " brutal-opts: { enabled: true, up: 1000 Mbps, down: 1000 Mbps }"
                    + updated[closing:]
                )
                changed = True
        else:
            core = updated.rstrip("\r\n")
            updated = core + newline + (
                f"{indent}smux:{newline}"
                f"{indent}  enabled: true{newline}"
                f"{indent}  protocol: h2mux{newline}"
                f"{indent}  padding: true{newline}"
                f"{indent}brutal-opts:{newline}"
                f"{indent}  enabled: true{newline}"
                f"{indent}  up: 1000 Mbps{newline}"
                f"{indent}  down: 1000 Mbps{newline}"
            )
            changed = True
    return updated, changed


def patch_yaml_subscription(text: str, target_tags: set[str]) -> tuple[str, bool]:
    marker = re.compile(r'(?m)^[ \t]*- (?=(?:name:|\{name:))')
    matches = list(marker.finditer(text))
    if not matches:
        return text, False
    pieces: list[str] = []
    changed = False
    cursor = 0
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        first_line_end = text.find("\n", match.end())
        first_line_end = len(text) if first_line_end < 0 else first_line_end + 1
        root_section = re.search(r'(?m)^[^\s#-][^\r\n]*$', text[first_line_end:])
        if root_section:
            end = min(end, first_line_end + root_section.start())
        pieces.append(text[cursor:match.start()])
        block = text[match.start():end]
        if yaml_supported(block, target_tags):
            block, block_changed = patch_yaml_block(block)
            changed = changed or block_changed
        pieces.append(block)
        cursor = end
    if not pieces:
        return text, False
    pieces.append(text[cursor:])
    return "".join(pieces), changed


def atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.yjl-tcp-brutal.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", errors="surrogateescape")
        shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def repair(log: Path) -> int:
    """Enable TCP Brutal on supported sing-box inbounds and structured subscriptions."""
    del log  # kept for signature compatibility with the TUI action dispatch
    if not Path("/sys/module/brutal").exists():
        print("当前内核模块 brutal 没有加载，先安装或加载 TCP Brutal，未修改 sing-box 文件。")
        return 1
    work_dir = Path("/etc/sing-box")
    conf_dir = work_dir / "conf"
    subscribe_dir = work_dir / "subscribe"
    if not conf_dir.is_dir():
        print(f"未找到 sing-box 配置目录：{conf_dir}")
        return 1

    inbound_names = {
        "14_ShadowTLS_inbounds.json",
        "15_shadowsocks_inbounds.json",
        "16_trojan_inbounds.json",
        "17_vmess-ws_inbounds.json",
        "18_vless-ws-tls_inbounds.json",
        "19_h2-reality_inbounds.json",
        "20_grpc-reality_inbounds.json",
    }
    target_tags: set[str] = set()
    file_updates: dict[Path, str] = {}
    for path in sorted(conf_dir.glob("*_inbounds.json")):
        if path.name not in inbound_names:
            continue
        original = path.read_text(encoding="utf-8", errors="surrogateescape")
        for tag in re.findall(r'"tag"\s*:\s*"([^"]+)"', original):
            target_tags.add(tag)
        updated, changed = patch_jsonc_inbound(original)
        if changed:
            file_updates[path] = updated

    if not target_tags:
        print("没有从支持的 sing-box 入站配置中找到节点 tag，未修改订阅。")
        return 1

    if subscribe_dir.is_dir():
        for path in sorted(subscribe_dir.iterdir()):
            if not path.is_file() or path.name == "qr":
                continue
            original = path.read_text(encoding="utf-8", errors="surrogateescape")
            if path.name == "sing-box":
                updated, changed = patch_singbox_subscription(original, target_tags)
            elif path.name in {"proxies", "clash", "clash2", "clash3"}:
                updated, changed = patch_yaml_subscription(original, target_tags)
            else:
                continue
            if changed:
                file_updates[path] = updated

    if not file_updates:
        print("已有支持协议的配置和结构化订阅均已启用 TCP Brutal，没有需要修改的文件。")
        return 0

    backup_dir = work_dir / f"tcp-brutal-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    backups: dict[Path, Path] = {}
    try:
        for path, updated in file_updates.items():
            relative = path.relative_to(work_dir)
            backup = backup_dir / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            backups[path] = backup
            atomic_write(path, updated)
        print(f"已更新 {len(file_updates)} 个文件；备份：{backup_dir}")
    except OSError as exc:
        print(f"写入 TCP Brutal 配置失败：{exc}")
        for path, backup in backups.items():
            shutil.copy2(backup, path)
        return 1

    binary = next((candidate for candidate in (
        Path("/etc/sing-box/sing-box"), Path("/usr/local/bin/sing-box"), Path("/usr/bin/sing-box")
    ) if candidate.is_file() and os.access(candidate, os.X_OK)), None)
    if binary:
        check = subprocess.run([str(binary), "check", "-C", str(conf_dir)], text=True, capture_output=True, timeout=60)
        if check.returncode:
            print("sing-box 配置检查失败，正在恢复本次修改。")
            for path, backup in backups.items():
                shutil.copy2(backup, path)
            print(check.stderr.strip() or check.stdout.strip())
            return check.returncode

    conf_changed = any(path.parent == conf_dir for path in file_updates)
    if conf_changed:
        if shutil.which("systemctl") and Path("/run/systemd/system").exists():
            reload_result = subprocess.run(["systemctl", "reload", "sing-box"], check=False, timeout=60)
        elif shutil.which("rc-service"):
            reload_result = subprocess.run(["rc-service", "sing-box", "restart"], check=False, timeout=60)
        else:
            reload_result = subprocess.CompletedProcess([], 1)
        if reload_result.returncode:
            print("配置文件已写入，但 sing-box 热加载失败；备份仍保留，请检查服务日志。")
            return reload_result.returncode
        print("sing-box 已重新加载 TCP Brutal 配置。")
    print("TCP Brutal 配置和结构化订阅修复完成。")
    return 0
