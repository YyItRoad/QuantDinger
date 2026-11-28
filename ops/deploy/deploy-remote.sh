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
: "${DISCIPLINE_AUTO_EXIT_ENABLED:=false}"
: "${DISCIPLINE_EXIT_FROM:=}"
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
preview_overlay="$DEPLOY_DIR/docker-compose.discipline-preview.yml"
if [[ -f "$preview_overlay" ]]; then
  compose+=(-f "$preview_overlay")
fi
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
verify_discipline_configuration() {
  "$@" python - "$DISCIPLINE_OBSERVATION_USER_ID" "$DISCIPLINE_AUTO_EXIT_ENABLED" "$DISCIPLINE_EXIT_FROM" <<'PY'
import os
import sys
from dotenv import load_dotenv

load_dotenv(os.getenv("QUANTDINGER_ENV_FILE") or "/app/.env", override=False)
disabled = ("TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED", "TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED")
expected = dict.fromkeys(disabled, "false")
expected.update(TRADE_DISCIPLINE_USER_ID=sys.argv[1], TRADE_DISCIPLINE_OBSERVATION_ENABLED="true",
                TRADE_DISCIPLINE_AUTO_EXIT_ENABLED=sys.argv[2],
                TRADE_DISCIPLINE_EXIT_FROM=sys.argv[3] if sys.argv[2] == "true" else "",
                TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL="true", TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS="24")
mismatched = [name for name, value in expected.items() if os.getenv(name) != value]
if mismatched:
    raise SystemExit("Unexpected trade-discipline preview configuration: " + ", ".join(mismatched))
if sys.argv[2] == "true":
    from app.services.settings.capabilities import is_live_trading_enabled
    from app.trade_discipline.exit import exit_start_from_env
    if not is_live_trading_enabled() or exit_start_from_env() is None:
        raise SystemExit("Discipline exits are not executable with the active global capability or cutoff")
print("Trade-discipline configuration verified: auto_exit=" + sys.argv[2] + "; adoption=false; entry_guard=false")
PY
}
preview_services=(backend scheduler-worker trading-worker)
# 自动退出必须显式给出用户和含时区的起效范围，不代替用户启用全局实盘能力。
[[ "$DISCIPLINE_AUTO_EXIT_ENABLED" == true || "$DISCIPLINE_AUTO_EXIT_ENABLED" == false ]] || die "Invalid automatic exit mode."
if [[ "$DISCIPLINE_AUTO_EXIT_ENABLED" == true ]]; then
  [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]] || die "Automatic exits require a discipline user id."
  "${compose[@]}" exec -T backend python - "$DISCIPLINE_EXIT_FROM" <<'PY'
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(os.getenv("QUANTDINGER_ENV_FILE") or "/app/.env", override=False)
from app.services.settings.capabilities import is_live_trading_enabled
try:
    cutoff = datetime.fromisoformat(sys.argv[1])
    if cutoff.utcoffset() is None:
        raise ValueError()
except ValueError:
    raise SystemExit("Automatic exits require a timezone-aware opening cutoff")
if not is_live_trading_enabled():
    raise SystemExit("Global live-trading capability is off; deployment will not enable it")
print("Automatic exit input and existing global capability verified")
PY
fi
if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  # 生产预览仅允许观察指定用户。先核实用户、现有计划事件表与数据库备份。
  [[ "$DISCIPLINE_OBSERVATION_USER_ID" =~ ^[1-9][0-9]{0,9}$ ]] || die "Invalid discipline observation user id."
  [[ -w "$DEPLOY_DIR" && -w "$ENV_FILE" ]] || die "Deploy account cannot atomically update the active .env."
  observation_ready=$("${compose[@]}" exec -T postgres sh -c '
    PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=5000" \
      psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -c "SELECT EXISTS (SELECT 1 FROM qd_users WHERE id=$1) AND
                 COALESCE(has_table_privilege(to_regclass(\$\$public.qd_trade_plan_events\$\$), \$\$SELECT\$\$), false) AND
                 COALESCE(has_table_privilege(to_regclass(\$\$public.qd_strategy_notifications\$\$), \$\$SELECT\$\$), false) AND
                 COALESCE(has_table_privilege(to_regclass(\$\$public.qd_strategy_notifications\$\$), \$\$INSERT\$\$), false) AND
                 COALESCE(has_table_privilege(to_regclass(\$\$public.qd_strategy_notifications\$\$), \$\$UPDATE\$\$), false)"
  ' sh "$DISCIPLINE_OBSERVATION_USER_ID")
  [[ "$observation_ready" == "t" ]] || die "Discipline user, plan-event table or notification persistence unavailable."
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
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_AUTO_EXIT_ENABLED" "$DISCIPLINE_AUTO_EXIT_ENABLED"
  if [[ "$DISCIPLINE_AUTO_EXIT_ENABLED" == true ]]; then
    env_set "$ENV_FILE" "TRADE_DISCIPLINE_EXIT_FROM" "$DISCIPLINE_EXIT_FROM"
  else
    env_set "$ENV_FILE" "TRADE_DISCIPLINE_EXIT_FROM" ""
  fi
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED" "false"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED" "false"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_OBSERVATION_ENABLED" "true"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL" "true"
  env_set "$ENV_FILE" "TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS" "24"
  # 直接向三个应用进程传递预览配置，兼容旧版 Compose 单独挂载 backend.env 的部署。
  # 重写脚本拥有的专用覆盖文件，保证旧版预览也获得最新业务配置，不修改 backend.env。
  overlay_existed=false
  [[ ! -f "$preview_overlay" ]] || overlay_existed=true
  {
    preview_tmp=$(mktemp "$DEPLOY_DIR/.discipline-preview.XXXXXX")
    cat > "$preview_tmp" <<'YAML'
x-discipline-environment: &discipline-environment
  TRADE_DISCIPLINE_USER_ID: ${TRADE_DISCIPLINE_USER_ID:-}
  TRADE_DISCIPLINE_AUTO_EXIT_ENABLED: ${TRADE_DISCIPLINE_AUTO_EXIT_ENABLED:-false}
  TRADE_DISCIPLINE_EXIT_FROM: ${TRADE_DISCIPLINE_EXIT_FROM:-}
  TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED: ${TRADE_DISCIPLINE_AUTO_ADOPT_ENABLED:-false}
  TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED: ${TRADE_DISCIPLINE_ENTRY_GUARD_ENABLED:-false}
  TRADE_DISCIPLINE_OBSERVATION_ENABLED: ${TRADE_DISCIPLINE_OBSERVATION_ENABLED:-false}
  TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL: ${TRADE_DISCIPLINE_EXTERNAL_OPENINGS_AS_MANUAL:-false}
  TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS: ${TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS:-24}
services:
  backend:
    environment: *discipline-environment
  scheduler-worker:
    environment: *discipline-environment
  trading-worker:
    environment: *discipline-environment
YAML
    mv "$preview_tmp" "$preview_overlay"
  }
  if [[ "$overlay_existed" == false ]]; then
    compose+=(-f "$preview_overlay")
  fi
  "${compose[@]}" config --quiet
  # 先用候选容器核对应用将读到的实际配置，再重启服务。
  for service in "${preview_services[@]}"; do
    log "Checking discipline configuration for $service before startup"
    verify_discipline_configuration "${compose[@]}" run --rm --no-deps -T --entrypoint "" "$service"
  done
fi

log "Restarting backend services..."
"${compose[@]}" up -d "${backend_services[@]}"

if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  for service in "${preview_services[@]}"; do
    log "Checking active discipline configuration for $service"
    verify_discipline_configuration "${compose[@]}" exec -T "$service"
  done
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

if [[ -n "$DISCIPLINE_OBSERVATION_USER_ID" ]]; then
  # 用实际 Scheduler 数据库角色只读核对复用表和既有幂等索引，不写测试业务记录。
  log "Checking active discipline schema with the Scheduler database role"
  "${compose[@]}" exec -T scheduler-worker python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(os.getenv("QUANTDINGER_ENV_FILE") or "/app/.env", override=False)
from app.utils.db import get_db_connection
from app.trade_discipline import origin, notifications
with get_db_connection() as conn:
    cur = conn.cursor()
    try:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("""SELECT
            has_table_privilege('public.qd_trade_plan_events', 'SELECT') AS can_read,
            has_table_privilege('public.qd_trade_plan_events', 'INSERT') AS can_append,
            (has_table_privilege('public.qd_strategy_notifications', 'SELECT')
             AND has_table_privilege('public.qd_strategy_notifications', 'INSERT')
             AND has_table_privilege('public.qd_strategy_notifications', 'UPDATE')) AS notices_durable,
            EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
                AND table_name='qd_trade_plan_events' AND column_name='payload' AND data_type='jsonb') AS payload_jsonb,
            EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.qd_trade_plan_events'::regclass
                AND contype='p') AS event_primary_key,
            EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.qd_trade_plan_events'::regclass
                AND contype='f' AND confrelid='public.qd_trade_plan_versions'::regclass) AS version_foreign_key,
            EXISTS (SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('public.uq_strategy_discipline_adoption')
                AND indisunique AND indisvalid) AS adoption_idempotency,
            EXISTS (SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('public.uq_quick_trade_discipline_exit')
                AND indisunique AND indisvalid) AS exit_idempotency""")
        checks = dict(cur.fetchone())
        if not all(checks.values()):
            raise SystemExit("Active discipline schema check failed: " + str(checks))
        print("Active discipline schema checks passed: " + str(checks))
        conn.rollback()
    finally:
        cur.close()
print("Active origin and notification modules imported")
PY
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
