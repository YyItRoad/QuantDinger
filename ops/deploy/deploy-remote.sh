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
# GHCR requires lowercase repo names. We lowercase in bash instead of using
# the GitHub Actions `| lower` filter — that filter triggered a schema
# fallback that hid the workflow name and the Run workflow button.
REPO_OWNER="${REPO_OWNER,,}"
: "${REF:=main}"

log() { printf '[deploy-remote] %s\n' "$*"; }
die() { printf '[deploy-remote] ERROR: %s\n' "$*" >&2; exit 1; }

###############################################################################
# 1. Preflight                                                               ##
###############################################################################
command -v docker > /dev/null 2>&1 || die "docker is not installed (run ops/deploy/server-prep.md first)."
docker info > /dev/null 2>&1     || die "docker daemon is not reachable (is the service running?).

Try: sudo systemctl status docker
     sudo systemctl start docker"

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
# 4. Update project-root .env orchestration knobs (IMAGE_TAG, BACKEND_TAG)  ##
#    Pattern borrowed from install.sh:env_set (in-place replace or append). ##
#    Never touches backend.env or any secrets.                              ##
###############################################################################
env_set() {
  local file="$1" key="$2" value="$3"
  touch "$file"
  local tmp="${file}.tmp.$$"
  : > "$tmp"
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
env_set "$ENV_FILE" "IMAGE_TAG"   "$IMAGE_TAG"
env_set "$ENV_FILE" "BACKEND_TAG" "$IMAGE_TAG"
log "Pinned IMAGE_TAG=$IMAGE_TAG and BACKEND_TAG=$IMAGE_TAG in $ENV_FILE."

###############################################################################
# 5. Static compose validation                                              ##
###############################################################################
log "Validating compose file (catches .env typos before pulling images)..."
docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE" config -q

###############################################################################
# 6. Pull new backend image (5 services share the same image)              ##
###############################################################################
log "Pulling backend image for services that share quantdinger-backend:$IMAGE_TAG..."
docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE" pull \
  backend \
  trading-worker \
  scheduler-worker \
  celery-worker \
  celery-beat

###############################################################################
# 7. Run database migration (idempotent) and restart the 5 backend services ##
###############################################################################
log "Running database migration (idempotent — re-running is safe)..."
docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE" run --rm migration

log "Restarting backend services..."
docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE" up -d \
  backend \
  trading-worker \
  scheduler-worker \
  celery-worker \
  celery-beat

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
  log "WARN: backend did not become healthy within 60s. Inspect: docker compose -f $DEPLOY_DIR/$COMPOSE_FILE logs backend"
fi

###############################################################################
# 9. Pretty-print container status for the run log                          ##
###############################################################################
log "Service status:"
docker compose -f "$DEPLOY_DIR/$COMPOSE_FILE" ps \
  backend trading-worker scheduler-worker celery-worker celery-beat \
  || true

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
