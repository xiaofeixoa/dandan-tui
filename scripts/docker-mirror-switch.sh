#!/usr/bin/env bash
# Docker registry mirror probe and switcher for the YJL Linux TUI.
# It probes Docker Registry v2 authentication, optionally pulls a small image,
# then replaces only registry-mirrors in /etc/docker/daemon.json after consent.

set -Eeuo pipefail
IFS=$'\n\t'

readonly DEFAULT_IMAGE='alpine:3.21'
readonly DAEMON_JSON='/etc/docker/daemon.json'
readonly -a DEFAULT_CANDIDATES=(
  'https://docker.m.daocloud.io'
  'https://docker.1ms.run'
)

TEST_IMAGE="$DEFAULT_IMAGE"
PULL_TIMEOUT=90
MODE='interactive'
VERIFY_PULL=0
APPLY=0
ASSUME_YES=0
KEEP_TEST_IMAGE=0
declare -a CANDIDATES=()
declare -a CUSTOM_CANDIDATES=()
declare -a API_OK=()
declare -a PULL_OK=()
TMP_DIR=''
REGISTRY_PATH=''
REGISTRY_REFERENCE=''

cleanup() {
  [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"
}
trap cleanup EXIT

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '\n== %s ==\n' "$*"
}

usage() {
  cat <<'EOF'
Usage: sudo bash docker-mirror-switch.sh [options]

Options:
  --check                 Probe Registry APIs only. Does not pull or change Docker.
  --verify-pull           Pull the test image from every API-valid source.
  --apply                 Replace registry-mirrors after successful pull validation.
  --yes                   Non-interactive confirmation. Valid only with --apply.
  --image IMAGE           Docker Hub image to test (default: alpine:3.21).
  --pull-timeout SEC      Limit each real pull test (default: 90 seconds).
  --candidate URL         Add a mirror candidate. May be used more than once.
  --keep-test-image       Keep source-prefixed test images after pull checks.
  --help                  Show this help text.

Examples:
  sudo bash docker-mirror-switch.sh
  sudo bash docker-mirror-switch.sh --check --image nginx:alpine
  sudo bash docker-mirror-switch.sh --verify-pull --image alpine:3.21
  sudo bash docker-mirror-switch.sh --apply --yes

Interactive mode probes first, then asks before real pulls and before changing
/etc/docker/daemon.json. --apply always requires successful real pull checks.
Only Docker Hub image names are accepted for testing. For example: alpine,
nginx:alpine, or library/alpine:3.21.

Docker's registry-mirrors setting accelerates Docker Hub references only. It
does not automatically proxy images explicitly hosted on ghcr.io, quay.io, or
other third-party registries.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

normalize_candidate() {
  local value="$1"
  value="${value%/}"
  [[ "$value" =~ ^https://[^[:space:]/]+$ ]] || die "Candidate must be an HTTPS registry base URL: $1"
  printf '%s\n' "$value"
}

parse_docker_hub_image() {
  local image="$1" name first last_slash=-1 last_colon=-1 i
  [[ -n "$image" && "$image" != */ ]] || die "Invalid image name: $image"

  if [[ "$image" == *@* ]]; then
    name="${image%@*}"
    REGISTRY_REFERENCE="${image#*@}"
    [[ -n "$REGISTRY_REFERENCE" ]] || die "Invalid image digest reference: $image"
  else
    name="$image"
    REGISTRY_REFERENCE='latest'
  fi

  for ((i = 0; i < ${#name}; i++)); do
    case "${name:i:1}" in
      /) last_slash=$i ;;
      :) last_colon=$i ;;
    esac
  done
  if [[ "$image" != *@* && $last_colon -gt $last_slash ]]; then
    REGISTRY_REFERENCE="${name:last_colon+1}"
    name="${name:0:last_colon}"
  fi

  first="${name%%/*}"
  [[ -n "$name" && -n "$REGISTRY_REFERENCE" ]] || die "Invalid image name: $image"
  if [[ "$name" == */* && ( "$first" == *.* || "$first" == *:* || "$first" == 'localhost' ) ]]; then
    die "Only Docker Hub images can be tested. Remove the explicit registry from: $image"
  fi
  if [[ "$name" != */* ]]; then
    REGISTRY_PATH="library/$name"
  else
    REGISTRY_PATH="$name"
  fi
}

http_request() {
  local headers="$1" body="$2" url="$3"
  curl --silent --show-error --location --retry 1 --connect-timeout 7 --max-time 30 \
    --dump-header "$headers" --output "$body" --write-out '%{http_code}' "$url"
}

header_value() {
  local name="$1" headers="$2"
  grep -i "^${name}:" "$headers" | head -n 1 | sed -e "s/^[^:]*:[[:space:]]*//" -e 's/\r$//'
}

json_token() {
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("access_token") or "")'
}

manifest_accept_header() {
  printf '%s' 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
}

probe_candidate() {
  local source="$1" headers body status auth realm service token token_headers token_body final_status content_type
  headers="$TMP_DIR/headers.$RANDOM"
  body="$TMP_DIR/body.$RANDOM"
  token_headers="$TMP_DIR/token-headers.$RANDOM"
  token_body="$TMP_DIR/token.$RANDOM"

  status=$(curl --silent --show-error --location --retry 1 --connect-timeout 7 --max-time 30 \
    --dump-header "$headers" --output "$body" --write-out '%{http_code}' \
    -H "Accept: $(manifest_accept_header)" \
    "$source/v2/$REGISTRY_PATH/manifests/$REGISTRY_REFERENCE") || {
      printf 'API FAIL (network error)\n'
      return 1
    }

  if [[ "$status" == '200' ]]; then
    content_type=$(header_value 'content-type' "$headers")
    if [[ "$content_type" == *manifest* || "$content_type" == *image.index* ]]; then
      printf 'API OK (anonymous manifest)\n'
      return 0
    fi
    printf 'API FAIL (HTTP 200, but not an image manifest: %s)\n' "${content_type:-unknown}"
    return 1
  fi

  [[ "$status" == '401' ]] || {
    printf 'API FAIL (HTTP %s)\n' "$status"
    return 1
  }
  auth=$(header_value 'www-authenticate' "$headers")
  [[ "$auth" == Bearer* ]] || {
    printf 'API FAIL (HTTP 401 without Docker Bearer challenge)\n'
    return 1
  }
  realm=$(printf '%s\n' "$auth" | sed -n 's/.*realm="\([^"]*\)".*/\1/p')
  service=$(printf '%s\n' "$auth" | sed -n 's/.*service="\([^"]*\)".*/\1/p')
  [[ -n "$realm" && -n "$service" ]] || {
    printf 'API FAIL (invalid Bearer challenge)\n'
    return 1
  }

  http_request "$token_headers" "$token_body" "$realm?service=$service&scope=repository%3A$REGISTRY_PATH%3Apull" >/dev/null || {
    printf 'API FAIL (token request failed)\n'
    return 1
  }
  token=$(json_token < "$token_body" 2>/dev/null || true)
  [[ -n "$token" ]] || {
    printf 'API FAIL (token missing)\n'
    return 1
  }
  final_status=$(curl --silent --show-error --location --retry 1 --connect-timeout 7 --max-time 30 \
    --dump-header "$headers" --output "$body" --write-out '%{http_code}' \
    -H "Accept: $(manifest_accept_header)" -H "Authorization: Bearer $token" \
    "$source/v2/$REGISTRY_PATH/manifests/$REGISTRY_REFERENCE") || {
      printf 'API FAIL (authenticated manifest request failed)\n'
      return 1
    }
  content_type=$(header_value 'content-type' "$headers")
  if [[ "$final_status" == '200' && ( "$content_type" == *manifest* || "$content_type" == *image.index* ) ]]; then
    printf 'API OK (Bearer authentication and manifest)\n'
    return 0
  fi
  printf 'API FAIL (authenticated HTTP %s)\n' "$final_status"
  return 1
}

pull_test() {
  local source="$1" source_host source_image before=0
  source_host="${source#https://}"
  if [[ "$TEST_IMAGE" == *@* ]]; then
    source_image="$source_host/$REGISTRY_PATH@$REGISTRY_REFERENCE"
  else
    source_image="$source_host/$REGISTRY_PATH:$REGISTRY_REFERENCE"
  fi
  if docker image inspect "$source_image" >/dev/null 2>&1; then
    before=1
  fi
  # timeout --foreground 是 GNU 专属；busybox 的 timeout 不认识该参数，先探测再降级。
  local timeout_cmd=(timeout --foreground)
  if ! timeout --foreground true 2>/dev/null; then
    timeout_cmd=(timeout)
  fi
  if "${timeout_cmd[@]}" "${PULL_TIMEOUT}s" docker pull "$source_image"; then
    printf 'PULL OK: %s\n' "$source_image"
    if [[ $before -eq 0 && $KEEP_TEST_IMAGE -eq 0 ]]; then
      docker image rm "$source_image" >/dev/null 2>&1 || true
      printf 'Removed test tag: %s\n' "$source_image"
    fi
    return 0
  fi
  printf 'PULL FAIL or timeout after %ss: %s\n' "$PULL_TIMEOUT" "$source_image" >&2
  return 1
}

confirm() {
  local prompt="$1" answer
  [[ $ASSUME_YES -eq 1 ]] && return 0
  read -r -p "$prompt [y/N]: " answer
  [[ "$answer" == 'y' || "$answer" == 'Y' || "$answer" == 'yes' || "$answer" == 'YES' ]]
}

rewrite_daemon_config() {
  local staged="$1"
  shift
  python3 - "$DAEMON_JSON" "$staged" "$@" <<'PY'
import json
import os
import sys

source, destination, *mirrors = sys.argv[1:]
data = {}
if os.path.exists(source):
    with open(source, encoding='utf-8') as fh:
        data = json.load(fh)
if not isinstance(data, dict):
    raise SystemExit('daemon.json must contain a JSON object')
data['registry-mirrors'] = mirrors
with open(destination, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, indent=2, ensure_ascii=True)
    fh.write('\n')
PY
}

apply_mirrors() {
  local staged backup timestamp image_present=0
  [[ ${#PULL_OK[@]} -gt 0 ]] || die 'Refusing to apply: no candidate passed a real docker pull.'
  [[ $EUID -eq 0 ]] || die '--apply must run as root.'
  mkdir -p /etc/docker
  staged=$(mktemp /etc/docker/daemon.json.new.XXXXXX)
  rewrite_daemon_config "$staged" "${PULL_OK[@]}"
  python3 -m json.tool "$staged" >/dev/null || die 'Generated daemon.json is invalid JSON.'

  if [[ -f "$DAEMON_JSON" ]]; then
    timestamp=$(date +%Y%m%d-%H%M%S)
    backup="${DAEMON_JSON}.bak.${timestamp}"
    cp -a "$DAEMON_JSON" "$backup"
    printf 'Backup: %s\n' "$backup"
  fi
  chmod 0644 "$staged"
  mv -f "$staged" "$DAEMON_JSON"
  printf 'Updated: %s\n' "$DAEMON_JSON"

  note 'Restarting Docker'
  systemctl restart docker
  if ! systemctl is-active --quiet docker; then
    if [[ -n "${backup:-}" && -f "$backup" ]]; then
      printf 'Docker 未能启动，正在自动恢复原 daemon.json ...\n' >&2
      cp -a "$backup" "$DAEMON_JSON"
      systemctl restart docker || true
      printf '已恢复原配置并再次重启 Docker；备份保留在：%s\n' "$backup" >&2
    fi
    die 'Docker did not become active after restart. Inspect: journalctl -u docker -n 100'
  fi
  docker info --format '{{range .RegistryConfig.Mirrors}}{{println .}}{{end}}'

  note "Final Docker pull through configured mirrors: $TEST_IMAGE"
  if docker image inspect "$TEST_IMAGE" >/dev/null 2>&1; then
    image_present=1
  fi
  docker pull "$TEST_IMAGE"
  if [[ $image_present -eq 0 && $KEEP_TEST_IMAGE -eq 0 ]]; then
    docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true
    printf 'Removed final test tag: %s\n' "$TEST_IMAGE"
  fi
  printf '\nSUCCESS: Docker mirror configuration replaced and verified.\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE='check' ;;
    --verify-pull) VERIFY_PULL=1 ;;
    --apply) APPLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --image)
      [[ $# -ge 2 ]] || die '--image needs a value.'
      TEST_IMAGE="$2"
      shift
      ;;
    --pull-timeout)
      [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]*$ ]] || die '--pull-timeout needs a positive whole number of seconds.'
      PULL_TIMEOUT="$2"
      shift
      ;;
    --candidate)
      [[ $# -ge 2 ]] || die '--candidate needs a value.'
      CUSTOM_CANDIDATES+=("$(normalize_candidate "$2")")
      shift
      ;;
    --keep-test-image) KEEP_TEST_IMAGE=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

[[ $ASSUME_YES -eq 0 || $APPLY -eq 1 ]] || die '--yes is only valid together with --apply.'
[[ $APPLY -eq 0 || $MODE != 'check' ]] || die '--apply cannot be combined with --check.'
[[ $APPLY -eq 0 ]] || VERIFY_PULL=1

require_command curl
require_command python3
if [[ $MODE != 'check' ]]; then
  require_command docker
  require_command timeout
fi
parse_docker_hub_image "$TEST_IMAGE"
TMP_DIR=$(mktemp -d)

CANDIDATES=("${DEFAULT_CANDIDATES[@]}" "${CUSTOM_CANDIDATES[@]}")

note "Registry API checks for $TEST_IMAGE"
for source in "${CANDIDATES[@]}"; do
  printf '%-38s ' "$source"
  if probe_candidate "$source"; then
    API_OK+=("$source")
  fi
done

if [[ ${#API_OK[@]} -eq 0 ]]; then
  die 'No candidate completed a valid Docker Registry API request. No changes made.'
fi

printf '\nAPI-valid candidates:\n'
printf '  %s\n' "${API_OK[@]}"

if [[ $MODE == 'check' ]]; then
  printf '\nCheck-only mode complete. No Docker image or configuration was changed.\n'
  exit 0
fi

if [[ $VERIFY_PULL -eq 0 ]]; then
  if confirm 'Run real docker pull validation for every API-valid candidate?'; then
    VERIFY_PULL=1
  fi
fi

if [[ $VERIFY_PULL -eq 1 ]]; then
  note "Real pull checks for $TEST_IMAGE"
  for source in "${API_OK[@]}"; do
    printf '\nSource: %s\n' "$source"
    if pull_test "$source"; then
      PULL_OK+=("$source")
    fi
  done
fi

if [[ ${#PULL_OK[@]} -eq 0 ]]; then
  printf '\nNo source passed a real pull, so daemon.json will not be changed.\n' >&2
  exit 2
fi

printf '\nPull-validated candidates that will be used in priority order:\n'
printf '  %s\n' "${PULL_OK[@]}"

if [[ $APPLY -eq 0 ]]; then
  if confirm 'Back up daemon.json, replace registry-mirrors, and restart Docker?'; then
    APPLY=1
  else
    printf 'Configuration unchanged. Re-run with --apply --yes to apply later.\n'
    exit 0
  fi
fi

apply_mirrors
