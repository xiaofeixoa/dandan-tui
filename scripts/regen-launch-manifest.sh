#!/usr/bin/env bash
# 重新生成根目录 SHA256SUMS（内容与 launch.sh 的 FILES 清单严格对应）。
# 改动清单内任何文件后运行一次；CI 与测试会检查过期。
set -Eeuo pipefail
cd -- "$(dirname -- "$0")/.."

command -v sha256sum >/dev/null 2>&1 || { printf '%s\n' '错误：缺少 sha256sum。' >&2; exit 1; }

mapfile -t FILES < <(grep -oP '^\s+"\K[^"]+(?=\|)' launch.sh)
[ "${#FILES[@]}" -gt 0 ] || { printf '%s\n' '错误：未从 launch.sh 解析到 FILES 清单。' >&2; exit 1; }

printf '' > SHA256SUMS
for file in "${FILES[@]}"; do
    [ -f "$file" ] || { printf '错误：清单文件不存在：%s\n' "$file" >&2; exit 1; }
    # Windows 的 sha256sum 默认输出 "*<path>"（二进制标记），规范化为 Linux 的两空格文本格式。
    sha256sum "$file" | sed 's/ \*/  /' >> SHA256SUMS
done

bash scripts/check-launch-manifest.sh
