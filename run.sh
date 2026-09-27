#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONUTF8=1
# 依次尝试 python3 / python，并验证真的能执行（Windows 上 python3 可能是商店占位程序）。
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    printf '%s\n' '错误：需要先安装 python3。' >&2
    exit 127
fi
exec "$PYTHON" "$SCRIPT_DIR/tui.py" "$@"
