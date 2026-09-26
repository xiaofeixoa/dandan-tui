"""Path constants, runtime identity, and scripts.json access."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# This module lives in <repo>/yjl_tui/, so the workspace root is one level up.
APP_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = APP_DIR.parent
CONFIG = APP_DIR / "scripts.json"
TCP_PROFILES = APP_DIR / "tcp_profiles.json"

VERSION = "1.2.0"


def _effective_uid() -> int:
    """Return the POSIX uid, or -1 on platforms without geteuid (e.g. Windows)."""
    getter = getattr(os, "geteuid", None)
    return getter() if getter else -1


IS_ROOT = _effective_uid() == 0
CACHE = Path(os.environ.get("YJL_TUI_CACHE_DIR", "/var/cache/yjl-tui" if IS_ROOT else Path.home() / ".cache/yjl-tui"))
LOGS = Path(os.environ.get("YJL_TUI_LOG_DIR", "/var/log/yjl-tui" if IS_ROOT else Path.home() / ".local/state/yjl-tui"))
STATE = Path(os.environ.get("YJL_TUI_STATE_DIR", "/var/lib/yjl-tui" if IS_ROOT else Path.home() / ".local/state/yjl-tui"))
WELCOME_MARKER = STATE / ".welcome-shown"

DOMAIN_LATENCY_HOSTS = [
    "gateway.icloud.com", "itunes.apple.com", "swdist.apple.com", "swcdn.apple.com",
    "updates.cdn-apple.com", "mensura.cdn-apple.com", "osxapps.itunes.apple.com",
    "aod.itunes.apple.com", "download-installer.cdn.mozilla.net", "addons.mozilla.org",
    "s0.awsstatic.com", "d1.awsstatic.com", "cdn-dynmedia-1.microsoft.com",
    "images-na.ssl-images-amazon.com", "m.media-amazon.com", "player.live-video.net",
    "one-piece.com", "lol.secure.dyn.riotcdn.net", "www.lovelive-anime.jp",
    "academy.nvidia.com", "software.download.prss.microsoft.com", "dl.google.com",
    "www.google-analytics.com", "www.caltech.edu", "www.calstatela.edu", "www.suny.edu",
    "www.suffolk.edu", "www.python.org", "vuejs-jp.org", "vuejs.org", "zh-hk.vuejs.org",
    "react.dev", "www.java.com", "www.oracle.com", "www.mysql.com", "www.mongodb.com",
    "redis.io", "cname.vercel-dns.com", "vercel-dns.com", "www.swift.com", "www.cisco.com",
    "www.asus.com", "www.samsung.com", "www.amd.com", "www.umcg.nl", "www.fom-international.com",
    "www.u-can.co.jp", "github.io",
]


def read_config() -> dict:
    with CONFIG.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("categories"), list) or not isinstance(data.get("actions"), list):
        raise ValueError("scripts.json must contain categories and actions")
    return data


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value[:60] or "action"
