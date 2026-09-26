#!/usr/bin/env bash
# Maintain a reviewed local mirror of SagerNet sing-geosite rule_set assets.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VENDOR_DIR="$SCRIPT_DIR/rule-set"
VENDOR_ARCHIVE="$SCRIPT_DIR/rule-set.tar.gz"
MANIFEST="$SCRIPT_DIR/SHA256SUMS"
UPSTREAM_META="$SCRIPT_DIR/UPSTREAM.json"

UPSTREAM_ARCHIVE="${YJL_GEOSITE_ARCHIVE_URL:-https://codeload.github.com/SagerNet/sing-geosite/tar.gz/refs/heads/rule-set}"
UPSTREAM_RAW="${YJL_GEOSITE_UPSTREAM_RAW:-https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set}"
MIRROR_REPO="${YJL_TUI_REPO:-dandan8511/dandan-tui}"
MIRROR_REF="${YJL_TUI_REF:-main}"
MIRROR_RAW="${YJL_GEOSITE_MIRROR_RAW:-https://raw.githubusercontent.com/${MIRROR_REPO}/${MIRROR_REF}/scripts/geosite/rule-set}"
TARGET_DIR="${YJL_GEOSITE_RULE_DIR:-/etc/sing-box/rules}"

CURATED_RULESETS=(
    geosite-openai
    geosite-anthropic
    geosite-google-gemini
    geosite-google-deepmind
    geosite-netflix
    geosite-disney
    geosite-hbo
    geosite-primevideo
    geosite-youtube
    geosite-spotify
    geosite-telegram
    geosite-tiktok
    geosite-bilibili
)

WORK_DIR=""

cleanup() {
    [ -z "$WORK_DIR" ] || rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

notice() {
    printf '%s\n' "$*"
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || die "需要命令：$1"
}

fetch() {
    local url="$1" output="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 2 --connect-timeout 15 --max-time 180 "$url" -o "$output"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=30 -O "$output" "$url"
    else
        die "需要 curl 或 wget"
    fi
}

rule_name() {
    local name="$1"
    name="${name%.srs}"
    [[ "$name" =~ ^[a-z0-9][a-z0-9._@-]*$ ]] || die "非法规则集名称：$1"
    printf '%s\n' "$name"
}

looks_like_srs() {
    local path="$1"
    [ -s "$path" ] || return 1
    ! head -c 256 -- "$path" | grep -Eqi '<!doctype|<html|404: not found'
}

manifest_hash() {
    local name="$1"
    [ -s "$MANIFEST" ] || return 0
    awk -v expected="rule-set/${name}.srs" '$2 == expected { print $1; exit }' "$MANIFEST"
}

verify_mirror_file() {
    local name="$1" path="$2" expected
    expected="$(manifest_hash "$name")"
    [ -n "$expected" ] || return 1
    printf '%s  %s\n' "$expected" "$path" | sha256sum -c - >/dev/null 2>&1
}

# launch.sh keeps the complete reviewed mirror as a compressed archive.  Extract
# it only when both network sources failed, so normal rule refreshes stay small.
local_vendor_rule() {
    local name="$1" extracted
    if [ -s "$VENDOR_DIR/${name}.srs" ] && verify_mirror_file "$name" "$VENDOR_DIR/${name}.srs"; then
        printf '%s\n' "$VENDOR_DIR/${name}.srs"
        return 0
    fi
    [ -s "$VENDOR_ARCHIVE" ] || return 1
    need_command tar
    extracted="$WORK_DIR/vendor-rule-set"
    mkdir -p -- "$extracted"
    tar -xzf "$VENDOR_ARCHIVE" -C "$extracted" -- "rule-set/${name}.srs" 2>/dev/null || return 1
    [ -s "$extracted/rule-set/${name}.srs" ] || return 1
    verify_mirror_file "$name" "$extracted/rule-set/${name}.srs" || return 1
    printf '%s\n' "$extracted/rule-set/${name}.srs"
}

write_manifest() {
    local source_dir="$1" output="$2"
    (
        cd -- "$source_dir"
        find . -type f -name '*.srs' -printf '%P\n' | LC_ALL=C sort | while IFS= read -r item; do
            sha256sum -- "$item" | awk '{print $1 "  rule-set/" $2}'
        done
    ) > "$output"
}

upstream_commit() {
    if command -v git >/dev/null 2>&1; then
        git ls-remote https://github.com/SagerNet/sing-geosite.git refs/heads/rule-set 2>/dev/null | awk 'NR == 1 { print $1 }'
    fi
}

vendor_all() {
    need_command tar
    need_command sha256sum
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/yjl-geosite.XXXXXX")"

    local archive="$WORK_DIR/rule-set.tar.gz"
    local extracted="$WORK_DIR/extracted"
    local staged="$WORK_DIR/rule-set"
    local source_dir backup_dir count commit manifest_tmp metadata_tmp

    notice "正在从 SagerNet rule-set 分支下载完整 .srs 快照..."
    fetch "$UPSTREAM_ARCHIVE" "$archive"
    mkdir -p -- "$extracted"
    tar -xzf "$archive" -C "$extracted"
    source_dir="$(find "$extracted" -mindepth 1 -maxdepth 1 -type d -name 'sing-geosite-*' -print -quit)"
    [ -n "$source_dir" ] || die "上游压缩包结构不符合预期"

    mkdir -p -- "$staged"
    while IFS= read -r -d '' item; do
        local relative="${item#"$source_dir"/}"
        mkdir -p -- "$staged/$(dirname -- "$relative")"
        cp -- "$item" "$staged/$relative"
    done < <(find "$source_dir" -type f -name '*.srs' -print0)

    count="$(find "$staged" -type f -name '*.srs' | wc -l | tr -d ' ')"
    [ "$count" -gt 100 ] || die "只提取到 $count 个 .srs，拒绝覆盖现有镜像"
    manifest_tmp="$WORK_DIR/SHA256SUMS"
    write_manifest "$staged" "$manifest_tmp"
    [ -s "$manifest_tmp" ] || die "规则集校验清单生成失败"

    commit="$(upstream_commit)"
    metadata_tmp="$WORK_DIR/UPSTREAM.json"
    printf '{\n  "upstream": "https://github.com/SagerNet/sing-geosite",\n  "branch": "rule-set",\n  "commit": "%s",\n  "updated_at_utc": "%s",\n  "rule_set_count": %s\n}\n' \
        "${commit:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$count" > "$metadata_tmp"

    backup_dir="${VENDOR_DIR}.previous.$$"
    [ ! -e "$VENDOR_DIR" ] || mv -- "$VENDOR_DIR" "$backup_dir"
    mv -- "$staged" "$VENDOR_DIR"
    mv -- "$manifest_tmp" "$MANIFEST"
    mv -- "$metadata_tmp" "$UPSTREAM_META"
    [ ! -e "$backup_dir" ] || rm -rf -- "$backup_dir"

    notice "已更新本地 geosite 镜像：$count 个 .srs"
    notice "上游提交：${commit:-unknown}"
    notice "请检查：git diff --stat -- scripts/geosite"
    notice "确认后再自行执行 git add / git commit / git push。"
}

sync_one() {
    local requested="$1" name candidate source="" existing local_source
    name="$(rule_name "$requested")"
    candidate="$WORK_DIR/${name}.srs"
    existing="$TARGET_DIR/${name}.srs"

    if fetch "$UPSTREAM_RAW/${name}.srs" "$candidate" 2>/dev/null && looks_like_srs "$candidate"; then
        source="SagerNet 上游"
    else
        rm -f -- "$candidate"
        if fetch "$MIRROR_RAW/${name}.srs" "$candidate" 2>/dev/null \
            && looks_like_srs "$candidate" \
            && verify_mirror_file "$name" "$candidate"; then
            source="dandan-tui GitHub 镜像"
        elif local_source="$(local_vendor_rule "$name")"; then
            cp -- "$local_source" "$candidate"
            source="本地仓库镜像"
        else
            if [ -s "$existing" ]; then
                notice "[保留] $name：上游和镜像均不可用，未覆盖已有本地规则"
                return 0
            fi
            notice "[失败] $name：上游、GitHub 镜像和本地仓库镜像均不可用"
            return 1
        fi
    fi

    install -m 0644 -- "$candidate" "$existing"
    notice "[通过] $name：已从 $source 更新到 $existing"
}

sync_rules() {
    if [[ "$TARGET_DIR" == /etc/sing-box/* || "$TARGET_DIR" == /etc/sing-box ]]; then
        [ "${EUID}" -eq 0 ] || die "同步到 $TARGET_DIR 需要 root 权限"
    fi
    need_command sha256sum
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/yjl-geosite.XXXXXX")"
    mkdir -p -- "$TARGET_DIR"

    local requested=()
    if [ "$#" -eq 0 ]; then
        requested=("${CURATED_RULESETS[@]}")
    else
        requested=("$@")
    fi

    local rule failed=0
    for rule in "${requested[@]}"; do
        sync_one "$rule" || failed=1
    done

    if [ "$failed" -ne 0 ]; then
        die "部分规则没有可用来源；已有规则已被保留，未写入不完整文件"
    fi
    notice "规则同步完成。此动作只刷新 $TARGET_DIR，不修改 /etc/sing-box/conf。"
}

usage() {
    cat <<'EOF'
用法：
  update.sh --vendor              本地维护：同步 SagerNet 的全部 .srs 到仓库
  update.sh --sync [规则集 ...]   服务器同步：优先上游，失败回退本仓库 GitHub 镜像

--sync 不带规则集时会同步内置 AI、流媒体和常用服务规则。
规则集名称可带或不带 .srs，例如：geosite-openai 或 geosite-openai.srs。
EOF
}

case "${1:---help}" in
    --vendor)
        [ "$#" -eq 1 ] || die "--vendor 不接受额外参数"
        vendor_all
        ;;
    --sync)
        shift
        sync_rules "$@"
        ;;
    --help|-h)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
