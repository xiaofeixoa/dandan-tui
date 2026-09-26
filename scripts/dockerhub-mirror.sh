#!/usr/bin/env bash
# Deploy and manage an official Docker Registry pull-through cache.
set -Eeuo pipefail
IFS=$'\n\t'

readonly STATE_DIR="${DOCKERHUB_MIRROR_STATE_DIR:-/var/lib/dockerhub-mirror}"
readonly COMPOSE_FILE="$STATE_DIR/compose.yaml"
readonly ENV_FILE="$STATE_DIR/.env"
readonly CACHE_DIR="$STATE_DIR/data"
readonly REPOSITORIES_DIR="$CACHE_DIR/docker/registry/v2/repositories"
readonly POLICY_FILE='/etc/dockerhub-mirror-policy.conf'
readonly POLICY_HELPER='/usr/local/sbin/dockerhub-mirror-cleanup'
readonly POLICY_SERVICE='/etc/systemd/system/dockerhub-mirror-cleanup.service'
readonly POLICY_TIMER='/etc/systemd/system/dockerhub-mirror-cleanup.timer'
readonly POLICY_LOCK='/run/lock/dockerhub-mirror-cache.lock'
readonly DAEMON_JSON='/etc/docker/daemon.json'
readonly IMAGE='registry:2'
readonly DEFAULT_REMOTE='https://registry-1.docker.io'
readonly DEFAULT_PORT_START=10305
readonly DEFAULT_PORT_END=10307

# 防御 DOCKERHUB_MIRROR_STATE_DIR 被置空或指到系统目录：下面的 rm -rf 以它为前缀。
case "$STATE_DIR" in
  ""|"/"|"/bin"|"/boot"|"/dev"|"/etc"|"/home"|"/lib"|"/lib64"|"/proc"|"/root"|"/run"|"/sbin"|"/sys"|"/tmp"|"/usr"|"/var"|"/var/lib")
    printf '错误：DOCKERHUB_MIRROR_STATE_DIR 不安全：%s\n' "${STATE_DIR:-（空）}" >&2
    exit 1
    ;;
esac

MODE='menu'
PORT=''
LISTEN_ADDR='0.0.0.0'
REMOTE_URL="$DEFAULT_REMOTE"
MIRROR_URL=''
INSECURE=0
ASSUME_YES=0
TEST_IMAGE='alpine:3.21'
IMAGE_REPOSITORY=''
IMAGE_REFERENCE=''
DELETE_REPOSITORY=''
MAX_CACHE_GB=''
EXPIRE_DAYS=''

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
note() { printf '\n== %s ==\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }
confirm() {
  local answer
  [[ $ASSUME_YES -eq 1 ]] && return 0
  read -r -p "$1 [y/N]: " answer
  [[ "$answer" == y || "$answer" == Y || "$answer" == yes || "$answer" == YES ]]
}
usage() {
  cat <<'EOF'
用法：sudo bash dockerhub-mirror.sh [选项]

默认进入菜单。服务端（国外 VPS）示例：
  sudo bash dockerhub-mirror.sh --server --port 10305 --public-url http://198.51.100.10:10305
客户端（国内 Docker 主机）示例：
  sudo bash dockerhub-mirror.sh --client --mirror https://mirror.example.com

选项：
  --server                 部署/更新 Docker Hub pull-through cache
  --client                 配置本机 Docker 使用镜像缓存
  --status                 查看服务端容器或本机镜像配置
  --uninstall              删除服务端 Compose 项目（数据默认保留）
  --cache-list             列出服务端缓存仓库和占用
  --delete-repository NAME 删除一个缓存仓库并回收其无引用数据
  --clear-cache            清空全部 Docker Hub 缓存并重新启动 Mirror
  --configure-policy       设置自动过期和缓存限额（整库缓存轮换）
  --policy-run             供 systemd timer 调用，按已配置策略执行清理
  --port PORT              服务端监听 TCP 端口；默认自动选择 10305-10307
  --listen ADDRESS         服务端绑定地址（默认 0.0.0.0）
  --public-url URL         客户端应访问的完整 URL（服务端必填）
  --mirror URL             客户端镜像 URL（必填）
  --insecure               允许 HTTP 镜像；客户端会写 insecure-registries
  --image IMAGE            部署后拉取验证的 Docker Hub 镜像（默认 alpine:3.21）
  --max-cache-gb N         缓存达到 N GiB 时自动清空；0 表示关闭限额
  --expire-days N          缓存创建/上次清空 N 天后自动清空；0 表示关闭过期
  --yes                    跳过确认（仅自动化使用）
EOF
}

repository_name_valid() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._/-]*$ && "$1" != *'..'* && "$1" != /* ]]
}

ensure_cache_marker() {
  mkdir -p "$CACHE_DIR"
  [[ -s "$CACHE_DIR/.cache-created-at" ]] || date -u +%s >"$CACHE_DIR/.cache-created-at"
}

cache_size_bytes() {
  [[ -d "$CACHE_DIR" ]] || { printf '0\n'; return; }
  du -sb "$CACHE_DIR" 2>/dev/null | awk '{print $1}'
}

cache_size_human() {
  [[ -d "$CACHE_DIR" ]] && du -sh "$CACHE_DIR" 2>/dev/null | awk '{print $1}' || printf '0\n'
}

mirror_compose() {
  (cd "$STATE_DIR" && docker compose -f "$COMPOSE_FILE" "$@")
}

garbage_collect() {
  note '回收未引用的缓存层'
  docker run --rm --network none \
    -v "$CACHE_DIR:/var/lib/registry" \
    "$IMAGE" garbage-collect /etc/docker/registry/config.yml
}

cache_list() {
  [[ -d "$REPOSITORIES_DIR" ]] || {
    printf '当前没有已缓存的 Docker Hub 仓库。\n'
    printf '缓存总占用：%s\n' "$(cache_size_human)"
    return 0
  }
  note 'Docker Hub 缓存仓库'
  local repo count=0
  while IFS= read -r -d '' manifest_dir; do
    repo="${manifest_dir#"$REPOSITORIES_DIR/"}"
    repo="${repo%/_manifests}"
    printf '%-48s %s\n' "$repo" "$(du -sh "$REPOSITORIES_DIR/$repo" 2>/dev/null | awk '{print $1}')"
    ((count += 1))
  done < <(find "$REPOSITORIES_DIR" -type d -name _manifests -print0 2>/dev/null)
  [[ $count -gt 0 ]] || printf '当前没有已缓存的 Docker Hub 仓库。\n'
  printf '缓存总占用：%s（%s）\n' "$(cache_size_human)" "$CACHE_DIR"
}

restart_mirror() {
  mirror_compose up -d --no-build
}

delete_repository() {
  local repo="$1" repository_path
  need docker
  repository_name_valid "$repo" || die "无效仓库名：$repo"
  repository_path="$REPOSITORIES_DIR/$repo"
  [[ -d "$repository_path" ]] || die "缓存中不存在仓库：$repo"
  printf '将删除缓存仓库：%s\n该操作只删除 OVH Mirror 缓存，不会删除枣庄或其他客户端的本地镜像。\n' "$repo"
  confirm '停止 Mirror、删除该仓库并执行垃圾回收？' || { printf '未做修改。\n'; return 0; }
  mirror_compose stop
  rm -rf -- "$repository_path"
  garbage_collect
  restart_mirror
  printf '已删除缓存仓库：%s\n' "$repo"
}

clear_cache_internal() {
  local reason="$1"
  need docker
  [[ -f "$COMPOSE_FILE" ]] || die "未发现服务端 Compose：$COMPOSE_FILE"
  printf '清空 Docker Hub Mirror 全部缓存：%s\n' "$reason"
  mirror_compose stop
  rm -rf -- "$CACHE_DIR"
  mkdir -p "$CACHE_DIR"
  chmod 700 "$CACHE_DIR"
  ensure_cache_marker
  restart_mirror
  printf '缓存已清空，Mirror 已重新启动。\n'
}

clear_cache() {
  printf '将清空全部 Docker Hub 镜像缓存：%s\n客户端已拉取的镜像不会受影响；下次拉取会重新从 Docker Hub 缓存。\n' "$CACHE_DIR"
  confirm '确认清空全部缓存？' || { printf '未做修改。\n'; return 0; }
  clear_cache_internal '手动清空'
}

read_policy() {
  POLICY_MAX_BYTES=0
  POLICY_EXPIRE_DAYS=0
  [[ -f "$POLICY_FILE" ]] || return 0
  # The policy file is generated locally and has only these two integer fields.
  # shellcheck source=/dev/null
  source "$POLICY_FILE"
  [[ "${POLICY_MAX_BYTES:-}" =~ ^[0-9]+$ ]] || POLICY_MAX_BYTES=0
  [[ "${POLICY_EXPIRE_DAYS:-}" =~ ^[0-9]+$ ]] || POLICY_EXPIRE_DAYS=0
}

policy_status() {
  read_policy
  printf '自动过期：%s\n' "$([[ $POLICY_EXPIRE_DAYS -gt 0 ]] && printf '%s 天后清空整库缓存' "$POLICY_EXPIRE_DAYS" || printf '未启用')"
  if [[ $POLICY_MAX_BYTES -gt 0 ]]; then
    printf '缓存限额：%s\n' "$(numfmt --to=iec-i --suffix=B "$POLICY_MAX_BYTES" 2>/dev/null || printf '%s 字节' "$POLICY_MAX_BYTES")"
  else
    printf '缓存限额：未启用\n'
  fi
  if systemctl list-unit-files dockerhub-mirror-cleanup.timer >/dev/null 2>&1; then
    systemctl list-timers dockerhub-mirror-cleanup.timer --no-pager 2>/dev/null || true
  fi
}

write_policy_units() {
  install -m 700 "$0" "$POLICY_HELPER"
  cat >"$POLICY_SERVICE" <<EOF
[Unit]
Description=Docker Hub Mirror cache retention and quota check
After=docker.service

[Service]
Type=oneshot
ExecStart=$POLICY_HELPER --policy-run
EOF
  cat >"$POLICY_TIMER" <<'EOF'
[Unit]
Description=Run Docker Hub Mirror cache cleanup daily

[Timer]
OnCalendar=*-*-* 04:25:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
}

configure_policy() {
  need systemctl
  local max_gb="$MAX_CACHE_GB" expire_days="$EXPIRE_DAYS"
  if [[ -z "$max_gb" ]]; then read -r -p '缓存限额（GiB，0 为不限制）：' max_gb; fi
  if [[ -z "$expire_days" ]]; then read -r -p '自动过期（天，0 为不过期）：' expire_days; fi
  [[ "$max_gb" =~ ^[0-9]+$ && $max_gb -le 1024 ]] || die '缓存限额必须是 0-1024 的整数 GiB。'
  [[ "$expire_days" =~ ^[0-9]+$ && $expire_days -le 3650 ]] || die '过期天数必须是 0-3650 的整数。'
  printf '自动策略：缓存达到 %s；缓存创建/清空 %s 天后清空。\n' \
    "$([[ $max_gb -gt 0 ]] && printf '%s GiB' "$max_gb" || printf '不限额')" \
    "$([[ $expire_days -gt 0 ]] && printf '%s' "$expire_days" || printf '不过期')"
  printf '触发后会短暂停止 Mirror、清空整个缓存目录、再启动；不会删除客户端镜像。\n'
  confirm '写入自动清理策略？' || { printf '未做修改。\n'; return 0; }
  cat >"$POLICY_FILE" <<EOF
# Generated by dockerhub-mirror.sh
POLICY_MAX_BYTES=$((max_gb * 1024 * 1024 * 1024))
POLICY_EXPIRE_DAYS=$expire_days
EOF
  chmod 600 "$POLICY_FILE"
  write_policy_units
  if [[ $max_gb -eq 0 && $expire_days -eq 0 ]]; then
    systemctl disable --now dockerhub-mirror-cleanup.timer >/dev/null 2>&1 || true
    printf '自动策略已关闭。\n'
  else
    systemctl enable --now dockerhub-mirror-cleanup.timer
    printf '自动策略已启用，每天 04:25 检查一次。\n'
  fi
}

run_policy() {
  need flock
  exec 9>"$POLICY_LOCK"
  flock -n 9 || { printf '已有缓存清理任务在运行，跳过。\n'; return 0; }
  read_policy
  [[ $POLICY_MAX_BYTES -gt 0 || $POLICY_EXPIRE_DAYS -gt 0 ]] || return 0
  ensure_cache_marker
  local now created age_seconds size_bytes expire_seconds=0
  now=$(date -u +%s)
  created=$(cat "$CACHE_DIR/.cache-created-at" 2>/dev/null || stat -c %Y "$CACHE_DIR")
  [[ "$created" =~ ^[0-9]+$ ]] || created="$now"
  age_seconds=$((now - created))
  (( POLICY_EXPIRE_DAYS > 0 )) && expire_seconds=$((POLICY_EXPIRE_DAYS * 86400))
  size_bytes=$(cache_size_bytes)
  if (( expire_seconds > 0 && age_seconds >= expire_seconds )); then
    clear_cache_internal "自动过期：已保留 ${POLICY_EXPIRE_DAYS} 天"
  elif (( POLICY_MAX_BYTES > 0 && size_bytes >= POLICY_MAX_BYTES )); then
    clear_cache_internal "自动限额：当前 $(numfmt --to=iec-i --suffix=B "$size_bytes")"
  else
    printf '自动策略未触发：缓存 %s，保留 %s 秒。\n' "$(numfmt --to=iec-i --suffix=B "$size_bytes")" "$age_seconds"
  fi
}

port_free() {
  local p="$1"
  ! ss -H -ltn "sport = :$p" 2>/dev/null | grep -q . &&
    ! ss -H -lun "sport = :$p" 2>/dev/null | grep -q .
}
choose_port() {
  need ss
  if [[ -n "$PORT" ]]; then
    [[ "$PORT" =~ ^[0-9]+$ && $PORT -ge 1 && $PORT -le 65535 ]] || die "无效端口：$PORT"
    port_free "$PORT" || die "TCP 或 UDP 已占用端口：$PORT"
    return
  fi
  local p
  for ((p=DEFAULT_PORT_START; p<=DEFAULT_PORT_END; p++)); do
    if port_free "$p"; then PORT="$p"; return; fi
  done
  die "10305-10307 没有空闲 TCP/UDP 端口，请用 --port 指定。"
}

parse_image() {
  local value="$1" name
  [[ "$value" != */* || "${value%%/*}" != *.* && "${value%%/*}" != *:* && "${value%%/*}" != localhost ]] ||
    die "验证镜像只能来自 Docker Hub：$1"
  if [[ "$value" == *@* ]]; then
    name="${value%@*}"
    IMAGE_REFERENCE="${value#*@}"
  else
    name="$value"
    IMAGE_REFERENCE='latest'
    if [[ "$name" == *:* && "${name##*:}" != */* ]]; then
      IMAGE_REFERENCE="${name##*:}"
      name="${name%:*}"
    fi
  fi
  [[ -n "$name" && -n "$IMAGE_REFERENCE" ]] || die "无效验证镜像：$1"
  [[ "$name" == */* ]] && IMAGE_REPOSITORY="$name" || IMAGE_REPOSITORY="library/$name"
}

write_server_files() {
  local public_url="$1"
  mkdir -p "$CACHE_DIR"
  chmod 700 "$STATE_DIR" "$CACHE_DIR"
  ensure_cache_marker
  cat >"$COMPOSE_FILE" <<EOF
services:
  dockerhub-mirror:
    image: $IMAGE
    container_name: dockerhub-mirror
    restart: unless-stopped
    environment:
      REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY: /var/lib/registry
      REGISTRY_PROXY_REMOTEURL: $REMOTE_URL
      REGISTRY_HTTP_ADDR: 0.0.0.0:5000
      REGISTRY_STORAGE_DELETE_ENABLED: "false"
    volumes:
      - $CACHE_DIR:/var/lib/registry
    ports:
      - "$LISTEN_ADDR:$PORT:5000"
EOF
  cat >"$ENV_FILE" <<EOF
# Generated by dockerhub-mirror.sh. Keep this directory private.
MIRROR_PUBLIC_URL=$public_url
MIRROR_PORT=$PORT
MIRROR_IMAGE=$IMAGE
EOF
  chmod 600 "$ENV_FILE"
  chmod 644 "$COMPOSE_FILE"
}

server_status() {
  need docker
  if [[ -f "$COMPOSE_FILE" ]]; then
    (cd "$STATE_DIR" && docker compose -f "$COMPOSE_FILE" ps) || true
    printf '\n缓存目录：%s（当前 %s）\n' "$CACHE_DIR" "$(cache_size_human)"
    policy_status
  else
    printf '未发现服务端配置：%s\n' "$COMPOSE_FILE"
    if [[ -f "$DAEMON_JSON" ]]; then
      python3 - "$DAEMON_JSON" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
    print('本机 registry-mirrors：', data.get('registry-mirrors', []))
    print('本机 insecure-registries：', data.get('insecure-registries', []))
except Exception as e:
    print(f'无法读取 daemon.json：{e}')
PY
    fi
  fi
}

server_install() {
  need docker; need ss; need curl
  choose_port; parse_image "$TEST_IMAGE"
  local public_url="${MIRROR_URL:-}"
  [[ -n "$public_url" ]] || {
    local host
    host=$(hostname -I 2>/dev/null | awk '{print $1}')
    [[ -n "$host" ]] || host='<OVH公网IP或域名>'
    public_url="http://$host:$PORT"
    [[ $INSECURE -eq 1 ]] || printf '提示：当前未提供 HTTPS 域名，自动示例为 HTTP；正式接入建议使用域名和 TLS。\n'
  }
  [[ "$public_url" =~ ^https?://[^[:space:]/]+(:[0-9]+)?/?$ ]] || die "--public-url 必须是完整 URL"
  public_url="${public_url%/}"
  if [[ "$public_url" == http://* && $INSECURE -eq 0 ]]; then
    printf '警告：HTTP mirror 需要在国内客户端使用 --insecure，流量未加密。\n'
  fi
  note "服务端部署参数"
  printf '监听：%s:%s（TCP/UDP 均检查）\n远端：%s\n公开 URL：%s\n数据：%s/data\n' "$LISTEN_ADDR" "$PORT" "$REMOTE_URL" "$public_url" "$STATE_DIR"
  confirm '写入 Compose 并启动 Docker Hub 缓存？' || { printf '未做修改。\n'; return 0; }
  write_server_files "$public_url"
  (cd "$STATE_DIR" && docker compose -f "$COMPOSE_FILE" pull && docker compose -f "$COMPOSE_FILE" up -d)
  sleep 2
  docker inspect dockerhub-mirror --format '容器状态：{{.State.Status}}' 2>/dev/null || true
  if curl -fsS --max-time 10 "$public_url/v2/" >/dev/null; then
    printf 'Registry API：可访问\n'
  else
    printf 'Registry API：暂未通过，请检查防火墙、端口和 URL。\n' >&2
  fi
  local local_test_image="127.0.0.1:$PORT/$IMAGE_REPOSITORY:$IMAGE_REFERENCE"
  if docker pull "$local_test_image"; then
    printf 'Docker Hub 缓存验证：已通过 %s 经本机 Registry 的真实拉取\n' "$TEST_IMAGE"
    docker image rm "$local_test_image" >/dev/null 2>&1 || true
  else
    printf 'Docker Hub 缓存验证未通过；容器仍已启动，请检查 Registry 日志和 Docker Hub 连通性。\n' >&2
  fi
  printf '\n国内客户端配置示例：\n  sudo bash dockerhub-mirror.sh --client --mirror %s%s\n' "$public_url" "$(if [[ "$public_url" == http://* ]]; then printf ' --insecure'; fi)"
}

client_config() {
  need docker; need python3
  [[ -n "$MIRROR_URL" ]] || die '--mirror 需要完整的镜像 URL，例如 https://mirror.example.com:10305'
  MIRROR_URL="${MIRROR_URL%/}"
  [[ "$MIRROR_URL" =~ ^https?://[^[:space:]/]+(:[0-9]+)?$ ]] || die "无效 mirror URL：$MIRROR_URL"
  [[ "$MIRROR_URL" == https://* || $INSECURE -eq 1 ]] || die 'HTTP mirror 必须显式加 --insecure。'
  note "将配置本机 Docker"
  printf '镜像：%s\n配置文件：%s\n' "$MIRROR_URL" "$DAEMON_JSON"
  confirm '写入 daemon.json 并重启 Docker？' || { printf '已恢复，不修改 Docker。\n'; return 0; }
  local backup
  if [[ -f "$DAEMON_JSON" ]]; then
    backup="$DAEMON_JSON.bak.dockerhub-mirror.$(date +%Y%m%d%H%M%S)"
    cp -a "$DAEMON_JSON" "$backup"
  else
    backup='（原文件不存在）'
  fi
  python3 - "$DAEMON_JSON" "$MIRROR_URL" "$INSECURE" <<'PY'
import json, os, sys
path, mirror, insecure = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
try:
    with open(path, encoding='utf-8') as f: data=json.load(f)
except FileNotFoundError: data={}
except json.JSONDecodeError as e: raise SystemExit(f'daemon.json JSON 无效：{e}')
data['registry-mirrors']=[mirror]
if insecure:
    values=data.get('insecure-registries', [])
    host=mirror.split('://',1)[1]
    if host not in values: values.append(host)
    data['insecure-registries']=values
tmp=path+'.dockerhub-mirror.tmp'
with open(tmp,'w',encoding='utf-8') as f:
    json.dump(data,f,ensure_ascii=False,indent=2); f.write('\n')
os.chmod(tmp,0o644); os.replace(tmp,path)
PY
  if systemctl restart docker 2>/dev/null; then
    docker info --format 'Docker mirrors：{{json .RegistryConfig.Mirrors}}' 2>/dev/null || true
  else
    printf 'Docker 重启失败，正在自动恢复原 daemon.json ...\n' >&2
    if [[ -f "$backup" ]]; then
      cp -a "$backup" "$DAEMON_JSON"
      systemctl restart docker 2>/dev/null || true
      printf '已恢复原配置并再次重启 Docker；备份保留在：%s\n请检查：journalctl -u docker -n 100\n' "$backup" >&2
    else
      printf '没有可用备份，请手动检查：journalctl -u docker -n 100\n' >&2
    fi
    return 1
  fi
  printf '客户端配置完成。只对未显式指定其他 registry 的 Docker Hub 镜像生效。\n'
}

uninstall_server() {
  need docker
  [[ -f "$COMPOSE_FILE" ]] || { printf '未发现服务端 Compose。\n'; return 0; }
  printf '将停止并删除 dockerhub-mirror 容器，缓存数据保留在 %s/data。\n' "$STATE_DIR"
  confirm '继续？' || return 0
  (cd "$STATE_DIR" && docker compose -f "$COMPOSE_FILE" down)
  printf '容器已删除，缓存数据未删除。\n'
}

menu() {
  while :; do
    printf '\nDocker Hub Mirror（Registry pull-through cache）\n1. 部署/更新服务端缓存\n2. 配置本机 Docker 客户端\n3. 查看状态和自动策略\n4. 本地镜像缓存管理（查看/删除仓库）\n5. 自动过期和限额配置\n6. 清空全部缓存\n7. 卸载服务端容器（保留缓存）\n0. 返回\n'
    local choice
    read -r -p '请选择：' choice || return
    case "$choice" in
      1) server_install ;;
      2) client_config ;;
      3) server_status ;;
      4)
        cache_list
        local repo
        read -r -p '输入要删除的仓库名（例如 library/redis，直接 Enter 返回）：' repo
        [[ -z "$repo" ]] || delete_repository "$repo"
        ;;
      5) configure_policy ;;
      6) clear_cache ;;
      7) uninstall_server ;;
      0) return ;;
      *) printf '无效选择。\n' ;;
    esac
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) MODE='server' ;;
    --client) MODE='client' ;;
    --status) MODE='status' ;;
    --uninstall) MODE='uninstall' ;;
    --cache-list) MODE='cache-list' ;;
    --delete-repository) [[ $# -ge 2 ]] || die '--delete-repository 缺少仓库名'; MODE='delete-repository'; DELETE_REPOSITORY="$2"; shift ;;
    --clear-cache) MODE='clear-cache' ;;
    --configure-policy) MODE='configure-policy' ;;
    --policy-run) MODE='policy-run' ;;
    --port) [[ $# -ge 2 ]] || die '--port 缺少值'; PORT="$2"; shift ;;
    --listen) [[ $# -ge 2 ]] || die '--listen 缺少值'; LISTEN_ADDR="$2"; shift ;;
    --public-url|--mirror) [[ $# -ge 2 ]] || die "$1 缺少值"; MIRROR_URL="$2"; shift ;;
    --insecure) INSECURE=1 ;;
    --image) [[ $# -ge 2 ]] || die '--image 缺少值'; TEST_IMAGE="$2"; shift ;;
    --max-cache-gb) [[ $# -ge 2 ]] || die '--max-cache-gb 缺少值'; MAX_CACHE_GB="$2"; shift ;;
    --expire-days) [[ $# -ge 2 ]] || die '--expire-days 缺少值'; EXPIRE_DAYS="$2"; shift ;;
    --yes) ASSUME_YES=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "未知选项：$1" ;;
  esac
  shift
done

case "$MODE" in
  menu) menu ;;
  server) server_install ;;
  client) client_config ;;
  status) server_status ;;
  uninstall) uninstall_server ;;
  cache-list) cache_list ;;
  delete-repository) delete_repository "$DELETE_REPOSITORY" ;;
  clear-cache) clear_cache ;;
  configure-policy) configure_policy ;;
  policy-run) run_policy ;;
esac
