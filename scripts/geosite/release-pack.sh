#!/usr/bin/env bash
# 重新打包 scripts/geosite 发布物：rule-set.tar.gz + SHA256SUMS。
# 用途：把 .srs 二进制改为 GitHub Release 资产分发前，本地生成待上传文件。
# 与 scripts/geosite/update.sh 的格式约定保持一致：
#   - tar 内路径带 rule-set/ 前缀
#   - SHA256SUMS 行格式："<sha256>  rule-set/<name>.srs"，按文件名排序
set -Eeuo pipefail
cd -- "$(dirname -- "$0")"

command -v tar >/dev/null 2>&1 || { printf '%s\n' '错误：缺少 tar。' >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { printf '%s\n' '错误：缺少 sha256sum。' >&2; exit 1; }

[ -d rule-set ] || { printf '%s\n' '错误：未找到 rule-set 目录。' >&2; exit 1; }

staged="$(mktemp -d "${TMPDIR:-/tmp}/yjl-geosite-pack.XXXXXX")"
cleanup() { rm -rf -- "$staged"; }
trap cleanup EXIT

count=$(find rule-set -type f -name '*.srs' | wc -l)
[ "$count" -gt 0 ] || { printf '%s\n' '错误：rule-set 目录没有 .srs 文件。' >&2; exit 1; }

tar -czf "$staged/rule-set.tar.gz" -- rule-set
(
    cd rule-set
    find . -type f -name '*.srs' -printf '%P\n' | LC_ALL=C sort | while IFS= read -r item; do
        sha256sum -- "$item" | awk '{print $1 "  rule-set/" $2}'
    done
) > "$staged/SHA256SUMS"

# 校验打包结果自洽。
(
    cd "$staged"
    mkdir -p verify
    tar -xzf rule-set.tar.gz -C verify
    cd verify
    sha256sum -c -- ../SHA256SUMS >/dev/null
)

mv -f -- "$staged/rule-set.tar.gz" rule-set.tar.gz
mv -f -- "$staged/SHA256SUMS" SHA256SUMS

# 同步 UPSTREAM.json 的 rule_set_count；commit 字段仅在能联系上游时刷新。
tmp_json="$staged/UPSTREAM.json"
if command -v python3 >/dev/null 2>&1; then
    commit="$(git ls-remote https://github.com/SagerNet/sing-geosite.git refs/heads/rule-set 2>/dev/null | awk 'NR == 1 { print $1 }')"
    python3 - "$count" "$commit" > "$tmp_json" <<'PY'
import json, sys
from datetime import datetime, timezone
count, commit = sys.argv[1], sys.argv[2]
try:
    data = json.load(open("UPSTREAM.json", encoding="utf-8"))
except Exception:
    data = {}
data["upstream"] = "https://github.com/SagerNet/sing-geosite"
data["branch"] = "rule-set"
if commit:
    data["commit"] = commit
data["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
data["rule_set_count"] = int(count)
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
    mv -f -- "$tmp_json" UPSTREAM.json
fi

printf '已重新打包：%s 条规则；rule-set.tar.gz 与 SHA256SUMS 已更新。\n' "$count"
printf '如改为 Release 资产分发：上传这两个文件，并把仓库内松散 .srs 移除。\n'
