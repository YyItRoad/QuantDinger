#!/usr/bin/env bash
#
# QuantDinger backend remote-deploy script.
#
# Invoked by `.github/workflows/deploy.yml` after it pushes a freshly built
# `quantdinger-backend:$IMAGE_TAG` to the fork owner's GHCR namespace. Pulls the
# new image and restarts the backend services on this host.
#
# You can also run this script by hand from the server:
#   IMAGE_TAG=manual-abc1234 ./ops/deploy/deploy-remote.sh
#
# Required environment (set by the workflow, defaults are friendly to manual use):
#   IMAGE_TAG    - GHCR tag to deploy (e.g. v5.0.1 or manual-<short-sha>).
#   GHCR_IMAGE   - Full image ref, including registry and tag.
#   DEPLOY_DIR   - Working directory on this host. Default /opt/quantdinger.
#   COMPOSE_FILE - Compose filename (sibling of this script's source). Default
#                  docker-compose.ghcr.yml.
#   PRUNE        - "true" to run `docker image prune -f` after success.
#                  Anything else is treated as false.
#   REPO_OWNER   - GitHub owner whose raw GitHub the compose file is fetched
#                  from on first run. Default OpenByteInc (upstream). For your
#                  fork, set this to your fork's owner.

set -euo pipefail

: "${IMAGE_TAG:=manual-latest}"
: "${GHCR_IMAGE:=ghcr.io/openbyteinc/quantdinger-backend:${IMAGE_TAG}}"
: "${DEPLOY_DIR:=/opt/quantdinger}"
: "${COMPOSE_FILE:=docker-compose.ghcr.yml}"
: "${PRUNE:=false}"
: "${REPO_OWNER:=OpenByteInc}"
: "${DISCIPLINE_OBSERVATION_USER_ID:=}"
# GHCR requires lowercase repo names. We lowercase in bash instead of using
# the GitHub Actions `| lower` filter — that filter triggered a schema
# fallback that hid the workflow name and the Run workflow button.
REPO_OWNER="$(printf '%s' "$REPO_OWNER" | tr '[:upper:]' '[:lower:]')"
: "${REF:=main}"

log() { printf '[deploy-remote] %s\n' "$*"; }
die() { printf '[deploy-remote] ERROR: %s\n' "$*" >&2; exit 1; }

###############################################################################
# 1. Preflight                                                               ##
###############################################################################
command -v docker > /dev/null 2>&1 || die "docker is not installed (run ops/deploy/server-prep.md first)."
docker info > /dev/null 2>&1     || die "docker daemon is not reachable (is the service running?)."

mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"

log "Deploy target: $DEPLOY_DIR"
log "Image       : $GHCR_IMAGE"
log "Compose file: $COMPOSE_FILE"

###############################################################################
# 2. Ensure docker-compose.ghcr.yml is present                              ##
###############################################################################
if [ ! -f "$DEPLOY_DIR/$COMPOSE_FILE" ]; then
  log "Fetching $COMPOSE_FILE from $REPO_OWNER/QuantDinger@$REF..."
  curl -fsSL \
    "https://raw.githubusercontent.com/${REPO_OWNER}/QuantDinger/${REF}/${COMPOSE_FILE}" \
    -o "$DEPLOY_DIR/$COMPOSE_FILE" \
    || die "Failed to fetch $COMPOSE_FILE. Check REPO_OWNER ('$REPO_OWNER') and network access."
  log "Saved to $DEPLOY_DIR/$COMPOSE_FILE"
fi

compose=(docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE")
backend_services=(backend trading-worker scheduler-worker celery-worker celery-backtest-worker celery-beat)
configured_services=$("${compose[@]}" config --services) || die "Unable to resolve Compose services."
for service in migration "${backend_services[@]}"; do
  if ! grep -Fxq -- "$service" <<< "$configured_services"; then
    die "Compose service missing: $service. Update the server Compose file before deploying."
  fi
done

###############################################################################
# 3. backend.env must be a FILE (never a directory) — see                   ##
#    docker-compose.ghcr.yml header for the bind-mount landmine.            ##
###############################################################################
if [ ! -e "$DEPLOY_DIR/backend.env" ]; then
  log "Creating empty backend.env (will need ADMIN_USER/ADMIN_PASSWORD/SECRET_KEY before first real use)."
  touch "$DEPLOY_DIR/backend.env"
  chmod 600 "$DEPLOY_DIR/backend.env"
fi

if [ -d "$DEPLOY_DIR/backend.env" ]; then
  die "backend.env exists as a DIRECTORY. Docker will bind-mount it over /app/.env. Move or remove it, then re-run."
fi
[ -f "$DEPLOY_DIR/backend.env" ] || die "backend.env must be a regular file."

###############################################################################
# 4. Pin the backend independently of the shared image tag  ##
#    Pattern borrowed from install.sh:env_set (in-place replace or append). ##
#    Never touches backend.env or any secrets.                              ##
###############################################################################
env_set() {
  local file="$1" key="$2" value="$3"
  touch "$file"
  local tmp="${file}.tmp.$$"
  (umask 077; : > "$tmp")
  local replaced="false"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "${key}="*)
        if [ "$replaced" != "true" ]; then
          printf '%s=%s\n' "$key" "$value" >> "$tmp"
          replaced="true"
        fi
        ;;
      *)
        printf '%s\n' "$line" >> "$tmp"
        ;;
    esac
  done < "$file"
  if [ "$replaced" != "true" ]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

ENV_FILE="$DEPLOY_DIR/.env"
verify_discipline_preview() {
  "$@" python - "$DISCIPLINE_OBSERVATION_USER_ID" <<'PY'
import os
import sys
from dotenv import load_dotenv

load_dotenv(os.getenv("QUANTDINGER_ENV_FILE") or "/app/.env", override=False)
disabled = ("TRADE_DISCIPLINE_AUTO_EXIT_ENABLED", "TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED",
            "TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED")
if (os.getenv("TRADE_DISCIPLINE_USER_ID") != sys.argv[1]
        or os.getenv("TRADE_DISCIPLINE_OBSERVATION_ENABLED") != "true"
        or any(os.getenv(name) != "false" for name in disabled)):
    raise SystemExit("Unexpected trade-discipline preview configuration")
print("Trade-discipline observation on; automatic actions and entry guard off")
PY
}
if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  # 生产预览仅允许观察指定用户。先核实用户、现有计划事件表与数据库备份。
  [[ "$DISCIPLINE_OBSERVATION_USER_ID" =~ ^[1-9][0-9]{0,9}$ ]] || die "Invalid discipline observation user id."
  [[ -w "$DEPLOY_DIR" && -w "$ENV_FILE" ]] || die "Deploy account cannot atomically update the active .env."
  observation_ready=$("${compose[@]}" exec -T postgres sh -c '
    PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=5000" \
      psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -c "SELECT EXISTS (SELECT 1 FROM qd_users WHERE id=$1) AND
                 COALESCE(has_table_privilege(to_regclass(\$\$public.qd_trade_plan_events\$\$), \$\$SELECT\$\$), false)"
  ' sh "$DISCIPLINE_OBSERVATION_USER_ID")
  [[ "$observation_ready" == "t" ]] || die "Discipline preview user or existing plan-event table unavailable."
  # 使用部署账号的持久目录，避免依赖旧部署目录中的备份目录权限。
  backup_root="${XDG_STATE_HOME:-$HOME/.local/state}/quantdinger/backups"
  backup_dir=$(umask 077; mkdir -p "$backup_root"; mktemp -d "$backup_root/discipline-preview-XXXXXX")
  (umask 077
    "${compose[@]}" exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$backup_dir/postgres.dump"
  )
  "${compose[@]}" exec -T postgres pg_restore --list < "$backup_dir/postgres.dump" > /dev/null
  log "Verified preview PostgreSQL backup: $backup_dir"
fi
env_set "$ENV_FILE" "BACKEND_TAG" "$IMAGE_TAG"
log "Pinned BACKEND_TAG=$IMAGE_TAG in $ENV_FILE."

###############################################################################
# 5. Static compose validation                                              ##
###############################################################################
log "Validating compose file (catches .env typos before pulling images)..."
"${compose[@]}" config -q

###############################################################################
# 6. Pull new images for the migration and backend services              ##
###############################################################################
log "Pulling backend image for services that share quantdinger-backend:$IMAGE_TAG..."
"${compose[@]}" pull migration "${backend_services[@]}"

###############################################################################
# 7. Run database migration (idempotent) and restart backend services ##
###############################################################################
log "Running database migration (idempotent — re-running is safe)..."
"${compose[@]}" run --rm migration

if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_USER_ID" "$DISCIPLINE_OBSERVATION_USER_ID"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_AUTO_EXIT_ENABLED" "false"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED" "false"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED" "false"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_OBSERVATION_ENABLED" "true"
  # 先用候选容器核对应用将读到的实际配置，再重启服务。
  verify_discipline_preview "${compose[@]}" run --rm --no-deps -T --entrypoint "" scheduler-worker
fi

log "Restarting backend services..."
"${compose[@]}" up -d "${backend_services[@]}"

if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  verify_discipline_preview "${compose[@]}" exec -T scheduler-worker
fi

###############################################################################
# 8. Wait for backend /api/health to come up                                ##
###############################################################################
log "Waiting up to 60s for backend /api/health..."
healthy="false"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:5000/api/health > /dev/null 2>&1; then
    log "Backend healthy after ${i} attempt(s)."
    healthy="true"
    break
  fi
  sleep 2
done
if [ "$healthy" != "true" ]; then
  die "Backend did not become healthy within 60s. Inspect: docker compose -f $DEPLOY_DIR/$COMPOSE_FILE logs backend"
fi

###############################################################################
# 9. Pretty-print container status for the run log                          ##
###############################################################################
log "Service status:"
"${compose[@]}" ps "${backend_services[@]}" || true

###############################################################################
# 10. Optional cleanup                                                      ##
###############################################################################
if [ "$PRUNE" = "true" ]; then
  log "Pruning dangling images..."
  docker image prune -f
else
  log "Skipping image prune (set PRUNE=true to enable)."
fi

log "Done. IMAGE_TAG=$IMAGE_TAG is live."
