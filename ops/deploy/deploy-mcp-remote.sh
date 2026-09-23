#!/usr/bin/env bash
# Deploy only the QuantDinger MCP gateway. Existing backend/frontend/worker
# containers are not rebuilt or restarted.

set -euo pipefail

: "${IMAGE_TAG:=manual-latest}"
: "${GHCR_IMAGE:=ghcr.io/yyitroad/quantdinger-mcp:${IMAGE_TAG}}"
: "${DEPLOY_DIR:=/opt/quantdinger}"
: "${BASE_COMPOSE_FILE:=docker-compose.ghcr.yml}"
: "${MCP_COMPOSE_FILE:=docker-compose.mcp.ghcr.yml}"
: "${PRUNE:=false}"

log() { printf '[deploy-mcp] %s\n' "$*"; }
die() { printf '[deploy-mcp] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed."
docker info >/dev/null 2>&1 || die "docker daemon is not reachable."

cd "$DEPLOY_DIR"
[ -f "$BASE_COMPOSE_FILE" ] || die "$DEPLOY_DIR/$BASE_COMPOSE_FILE is missing."
[ -f "$MCP_COMPOSE_FILE" ] || die "$DEPLOY_DIR/$MCP_COMPOSE_FILE is missing."
[ -f mcp.env ] || die "$DEPLOY_DIR/mcp.env is missing. Create it from ops/deploy/MCP_DEPLOY_CN.md."
[ ! -d mcp.env ] || die "$DEPLOY_DIR/mcp.env must be a regular file, not a directory."

deploy_user="$(id -un)"
deploy_group="$(id -gn)"
if [ ! -r mcp.env ]; then
  owner="$(stat -c '%U:%G' mcp.env 2>/dev/null || printf 'unknown')"
  die "mcp.env is not readable by SSH deploy user '$deploy_user' (owner: $owner). Run as root: chown $deploy_user:$deploy_group $DEPLOY_DIR/mcp.env && chmod 600 $DEPLOY_DIR/mcp.env"
fi
if ! chmod 600 mcp.env 2>/dev/null; then
  mode="$(stat -c '%a' mcp.env 2>/dev/null || printf 'unknown')"
  [ "$mode" = "600" ] || die "mcp.env permissions are $mode and cannot be tightened by '$deploy_user'. Fix its ownership, then set mode 600."
  log "mcp.env is readable and already mode 600; continuing without changing its ownership."
fi

agent_token="$(sed -n 's/^QUANTDINGER_AGENT_TOKEN=//p' mcp.env | tail -n 1)"
mcp_auth_token="$(sed -n 's/^QUANTDINGER_MCP_AUTH_TOKEN=//p' mcp.env | tail -n 1)"
[ -n "$agent_token" ] || die "QUANTDINGER_AGENT_TOKEN is missing from mcp.env."
[ "${#mcp_auth_token}" -ge 32 ] || die "QUANTDINGER_MCP_AUTH_TOKEN in mcp.env must be at least 32 characters."
[ "$agent_token" != "$mcp_auth_token" ] || die "MCP auth token must differ from the Agent token."
unset agent_token mcp_auth_token

backend_running="$(docker inspect --format '{{.State.Running}}' quantdinger-backend 2>/dev/null || true)"
[ "$backend_running" = "true" ] || die "quantdinger-backend is not running on this host."

env_set() {
  local file="$1" key="$2" value="$3"
  touch "$file"
  local tmp="${file}.tmp.$$"
  : >"$tmp"
  local replaced="false"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "${key}="*)
        if [ "$replaced" != "true" ]; then
          printf '%s=%s\n' "$key" "$value" >>"$tmp"
          replaced="true"
        fi
        ;;
      *) printf '%s\n' "$line" >>"$tmp" ;;
    esac
  done <"$file"
  if [ "$replaced" != "true" ]; then
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  mv "$tmp" "$file"
}

image_repo="${GHCR_IMAGE%:*}"
ENV_FILE="$DEPLOY_DIR/.env"
env_set "$ENV_FILE" MCP_IMAGE "$image_repo"
env_set "$ENV_FILE" MCP_TAG "$IMAGE_TAG"

compose=(docker compose -f "$BASE_COMPOSE_FILE" -f "$MCP_COMPOSE_FILE")
log "Validating production Compose configuration..."
"${compose[@]}" config -q

log "Pulling $GHCR_IMAGE..."
docker pull "$GHCR_IMAGE"

log "Starting MCP without restarting its dependencies..."
"${compose[@]}" up -d --no-deps mcp

log "Waiting up to 75 seconds for the MCP container to become healthy..."
healthy="false"
for _ in $(seq 1 25); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' quantdinger-mcp 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    healthy="true"
    break
  fi
  if [ "$status" = "unhealthy" ] || [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
    docker logs --tail 80 quantdinger-mcp || true
    die "MCP container entered state: $status"
  fi
  sleep 3
done
[ "$healthy" = "true" ] || {
  docker logs --tail 80 quantdinger-mcp || true
  die "MCP container did not become healthy within 75 seconds."
}

log "MCP is healthy. Public routing will be provided by the frontend at https://trade.yangyang.fun/mcp."
docker ps --filter name=quantdinger-mcp --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

if [ "$PRUNE" = "true" ]; then
  docker image prune -f
fi
