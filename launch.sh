#!/usr/bin/env bash
set -Eeuo pipefail

REF="${YJL_TUI_REF:-main}"
# REF 会被拼进下载 URL，只允许安全的 ref 字符，防止畸形 URL。
case "$REF" in
    ""|*[!A-Za-z0-9._/-]*)
        printf '错误：YJL_TUI_REF 含不合法字符（只允许字母、数字、点、下划线、斜杠、连字符）：%s\n' "$REF" >&2
        exit 1
        ;;
esac
BASE_URL="https://raw.githubusercontent.com/xiaofeixoa/dandan-tui/${REF}"
# 默认不附加缓存穿透参数：raw.githubusercontent.com 自带 CDN 缓存，
# 需要强制拉最新时可设置 YJL_TUI_CACHE_BUSTER=任意值。
CACHE_BUSTER="${YJL_TUI_CACHE_BUSTER:-}"
# 离线安装：指向完整工程目录时直接复制、不访问网络（SHA256SUMS 校验照常执行）。
# 用于无法访问 GitHub 的 VPS：把工程目录整个传过去后
# YJL_TUI_LOCAL_SOURCE=/path/to/dandan-tui bash launch.sh
LOCAL_SOURCE="${YJL_TUI_LOCAL_SOURCE:-}"
CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}/dandan-tui"
MANIFEST="SHA256SUMS"

# 下载清单：路径|权限（700 可执行，600 数据/被 TUI 读取）。
# 新增菜单本地脚本时必须同步维护此清单和根目录 SHA256SUMS。
FILES=(
    "run.sh|700"
    "tui.py|600"
    "kernel_manager.py|600"
    "nginx_manager.py|600"
    "singbox_manager.py|600"
    "scripts.json|600"
    "tcp_profiles.json|600"
    "yjl_tui/__init__.py|600"
    "yjl_tui/paths.py|600"
    "yjl_tui/probes.py|600"
    "yjl_tui/tcp_brutal.py|600"
    "yjl_tui/doctor.py|600"
    "yjl_tui/tui.py|600"
    "scripts/install-tcp-brutal.sh|700"
    "scripts/tcp-brutal-manager.sh|700"
    "scripts/tcp-brutal/scripts/install_dkms.sh|700"
    "scripts/tcp-brutal/dkms.tar.gz|600"
    "scripts/fscarmen-sing-box.sh|600"
    "scripts/fscarmen-warp.sh|600"
    "scripts/nekoneko-tools.sh|700"
    "scripts/tcpfit/tcpfit.sh|700"
    "scripts/docker-mirror-switch.sh|700"
    "scripts/dockerhub-mirror.sh|700"
    "scripts/geosite/update.sh|700"
    "scripts/geosite/SHA256SUMS|600"
    "scripts/geosite/UPSTREAM.json|600"
    "scripts/geosite/rule-set.tar.gz|600"
    "scripts/kernel-installer/kernel_installer.sh|700"
    "scripts/kernel-installer/src/slib.sh|600"
    "scripts/kernel-installer/LICENSE|600"
    "scripts/kernel-installer/UPSTREAM.md|600"
    "scripts/ubuntu-mainline-signing-key.gpg|600"
    "scripts/ubuntu-mainline-signing-key.md|600"
    "tools/nft-forward/install.sh|700"
    "tools/nginx-ui/install.sh|700"
    "tools/nginx-ui/SHA256SUMS|600"
    "yjl-argo/yjl-argo.sh|700"
)

mkdir -p -- "$CACHE_ROOT"
TEMP_DIR="$(mktemp -d "${CACHE_ROOT}.download.XXXXXX")"

cleanup() {
    rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

download() {
    local name="$1"
    mkdir -p -- "${TEMP_DIR}/$(dirname -- "$name")"
    local url="${BASE_URL}/${name}"
    if [ -n "$CACHE_BUSTER" ]; then
        url="${url}?v=${CACHE_BUSTER}"
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 15 --max-time 180 "$url" -o "${TEMP_DIR}/${name}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 --timeout=30 -O "${TEMP_DIR}/${name}" "$url"
    else
        printf '%s\n' '错误：需要 curl 或 wget。' >&2
        exit 127
    fi
}

# 离线安装：YJL_TUI_LOCAL_SOURCE 指向完整工程目录时，直接复制而不访问网络。
# 目录里同样要有与 FILES 清单一致的 SHA256SUMS，校验照常执行。
acquire_files() {
    if [ -n "$LOCAL_SOURCE" ]; then
        if [ ! -d "$LOCAL_SOURCE" ]; then
            printf '错误：YJL_TUI_LOCAL_SOURCE 目录不存在：%s\n' "$LOCAL_SOURCE" >&2
            exit 1
        fi
        for entry in "${FILES[@]}"; do
            path="${entry%%|*}"
            mkdir -p -- "${TEMP_DIR}/$(dirname -- "$path")"
            cp -a -- "${LOCAL_SOURCE}/${path}" "${TEMP_DIR}/${path}"
        done
        # 校验清单本身也要复制（在线模式由 download "$MANIFEST" 单独获取）。
        cp -a -- "${LOCAL_SOURCE}/${MANIFEST}" "${TEMP_DIR}/${MANIFEST}"
        return 0
    fi
    download "$MANIFEST"
    for entry in "${FILES[@]}"; do
        download "${entry%%|*}"
    done
}

verify_downloads() {
    command -v sha256sum >/dev/null 2>&1 || {
        printf '%s\n' '警告：缺少 sha256sum，跳过下载完整性校验。' >&2
        return 0
    }
    (
        cd -- "$TEMP_DIR"
        if ! sha256sum -c -- "$MANIFEST" >/dev/null 2>&1; then
            printf '%s\n' '错误：SHA256SUMS 校验失败，下载内容与仓库清单不一致。' >&2
            printf '%s\n' '请重试；如需固定版本可设置 YJL_TUI_REF=提交SHA。' >&2
            exit 1
        fi
    )
}

run_as_root() {
    if [ "${EUID}" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        printf '%s\n' '错误：安装 Python 3 需要 root 权限或 sudo。' >&2
        return 1
    fi
}

install_python3() {
    printf '%s\n' '未检测到 python3，正在根据系统安装 Python 3 ...'
    if command -v apt-get >/dev/null 2>&1; then
        if ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update || \
           ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3; then
            return 1
        fi
    elif command -v apk >/dev/null 2>&1; then
        if ! run_as_root apk add --no-cache python3; then
            return 1
        fi
    elif command -v dnf >/dev/null 2>&1; then
        if ! run_as_root dnf install -y python3; then
            return 1
        fi
    elif command -v yum >/dev/null 2>&1; then
        if ! run_as_root yum install -y python3; then
            return 1
        fi
    else
        printf '%s\n' '错误：未识别 apt、apk、dnf 或 yum，无法自动安装 python3。' >&2
        return 1
    fi
    command -v python3 >/dev/null 2>&1
}

if ! command -v python3 >/dev/null 2>&1; then
    if ! install_python3 || ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' '错误：python3 自动安装失败，请手动安装后重试。' >&2
        exit 127
    fi
    printf '%s\n' "Python 3 已安装：$(python3 --version 2>&1)"
fi

# 1) 获取全部文件（在线下载或离线复制），然后逐项校验；任何失败都不会进入安装阶段。
acquire_files
verify_downloads

# 2) 在临时目录内设置权限（下载阶段事务化，坏文件不会污染现有缓存）。
for entry in "${FILES[@]}"; do
    path="${entry%%|*}"
    mode="${entry##*|}"
    chmod "$mode" "${TEMP_DIR}/${path}"
done

# 3) 原子安装：先在 staging 组装完整新版本，再整体替换 CACHE_ROOT。
STAGING="${CACHE_ROOT}.staging.$$"
OLD_ROOT="${CACHE_ROOT}.old.$$"
rm -rf -- "$STAGING"
mkdir -p -- "$STAGING"
for entry in "${FILES[@]}"; do
    path="${entry%%|*}"
    mkdir -p -- "$STAGING/$(dirname -- "$path")"
    mv -f -- "${TEMP_DIR}/${path}" "${STAGING}/${path}"
done
if mv -- "$CACHE_ROOT" "$OLD_ROOT" 2>/dev/null; then
    if mv -- "$STAGING" "$CACHE_ROOT"; then
        rm -rf -- "$OLD_ROOT"
    else
        printf '%s\n' '错误：新版本安装失败，已恢复原缓存。' >&2
        mv -- "$OLD_ROOT" "$CACHE_ROOT"
        exit 1
    fi
else
    mv -- "$STAGING" "$CACHE_ROOT"
fi

exec bash "${CACHE_ROOT}/run.sh" "$@"
