#!/usr/bin/env bash
# 校验根目录 SHA256SUMS 与 launch.sh 的下载清单一致且内容未过期。
# CI 与本地测试共用；任何一个被改动的文件忘记刷新清单都会在这里失败。
set -Eeuo pipefail
cd -- "$(dirname -- "$0")/.."

command -v sha256sum >/dev/null 2>&1 || { printf '%s\n' '错误：缺少 sha256sum。' >&2; exit 1; }

[ -f SHA256SUMS ] || { printf '%s\n' '错误：缺少 SHA256SUMS。' >&2; exit 1; }

# 1) launch.sh FILES 清单里的每个路径都必须出现在 SHA256SUMS。
missing=0
while IFS= read -r path; do
    if ! grep -q "  ${path}\$" SHA256SUMS; then
        printf 'SHA256SUMS 缺少清单文件：%s\n' "$path" >&2
        missing=1
    fi
done < <(grep -oP '^\s+"\K[^"]+(?=\|)' launch.sh)
[ "$missing" -eq 0 ] || exit 1

# 2) SHA256SUMS 内容与仓库当前文件一致。
sha256sum -c -- SHA256SUMS

printf 'SHA256SUMS 与 launch.sh 清单一致，内容校验通过。\n'
