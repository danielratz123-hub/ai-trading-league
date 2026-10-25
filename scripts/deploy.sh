#!/bin/sh
# Deploy the latest `main` from GitHub to this server, safely (ROADMAP 0.10).
# Run on the server in the project folder; GitHub Actions runs it over SSH
# after the tests pass on a push to main.
#
#   ./scripts/deploy.sh            deploy origin/main
#   ./scripts/deploy.sh <commit>   deploy that commit of main
#
# 1. waits until no league job is running (an agent turn can take minutes)
#    and stays out of the 09:45-10:05 ET window around the opening execute
# 2. remembers the current commit, checks out the new one
# 3. rebuilds and restarts (data and logins live in volumes and survive)
# 4. health check: every service healthy + the scheduler heartbeat
# 5. unhealthy -> rolls back to the previous commit, rebuilds, alerts, exit 1
# Telegram gets the result either way.
set -eu
if [ "${DEPLOY_CHECK_ONLY:-0}" = "1" ]; then
  cd "${DEPLOY_REPO:-.}"
else
  cd "${DEPLOY_REPO:-$(dirname "$0")/..}"
fi

TARGET="${1:-origin/main}"
MAX_WAIT=2400          # seconds to wait for a quiet moment

alert() {   # alert "<level>" "<text>"  (never fails the deploy)
  docker compose exec -T engine python -c \
    "import sys; from arena import alerts; alerts.send(sys.argv[2], level=sys.argv[1])" "$1" "$2" >/dev/null 2>&1 || true
}

# Only a commit id (or origin/main): this is what the GitHub key may pass.
case "$TARGET" in
  origin/main) ;;
  *) printf '%s' "$TARGET" | grep -Eq '^[0-9a-f]{7,40}$' || { echo "Not a commit id: $TARGET"; alert error "Deploy refused: target is not a commit id; nothing deployed."; exit 2; } ;;
esac

check_deploy_target() {
  if ! git fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main'; then
    msg="Refusing to deploy $TARGET: git fetch origin main failed"
    echo "$msg"
    alert "error" "$msg"
    exit 3
  fi
  if ! COMMIT=$(git rev-parse --verify "$TARGET^{commit}" 2>/dev/null) \
     || ! git merge-base --is-ancestor "$COMMIT" origin/main; then
    msg="Refusing to deploy $TARGET: it is not on origin/main"
    echo "$msg"
    alert "error" "$msg"
    exit 3
  fi
}

check_deploy_target
[ "${DEPLOY_CHECK_ONLY:-0}" = "1" ] && exit 0

busy() {    # a job is running, or we're near the opening execute
  hm=$(TZ=America/New_York date +%H%M)
  dow=$(TZ=America/New_York date +%u)
  if [ "$dow" -le 5 ] && [ "$hm" -ge 0945 ] && [ "$hm" -le 1005 ]; then return 0; fi
  n=$(docker compose exec -T engine python -c \
      "import sqlite3; c=sqlite3.connect('file:/app/data/arena.db?mode=ro', uri=True); print(c.execute('SELECT COUNT(*) FROM job_runs WHERE finished_at IS NULL').fetchone()[0])" \
      2>/dev/null || echo 0)
  [ "$n" -gt 0 ]
}

healthy() {
  for _ in $(seq 1 18); do       # up to 3 minutes
    sleep 10
    bad=$(docker compose ps --format '{{.Service}} {{.Status}}' | grep -E 'engine|api|setup' | grep -vc '(healthy)' || true)
    if [ "$bad" -eq 0 ] && docker compose exec -T engine python -m arena.main health --scheduler >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

waited=0
while busy; do
  [ "$waited" -ge "$MAX_WAIT" ] && { echo "Still busy after ${MAX_WAIT}s; not deploying. Re-run the workflow later."; exit 75; }
  echo "A league job is running (or it's around the open); waiting..."
  sleep 30; waited=$((waited + 30))
done

PREV=$(git rev-parse HEAD)
git checkout --quiet -B main "$COMMIT"
NEW=$(git rev-parse --short HEAD)
echo "== Deploying $NEW (was $(git rev-parse --short "$PREV"))"
docker compose up -d --build
if healthy; then
  echo "== Deployed $NEW and healthy"
  alert good "Deployed $NEW ($(git log -1 --format=%s | cut -c1-80)). All services healthy."
  exit 0
fi

echo "== $NEW is UNHEALTHY: rolling back to $(git rev-parse --short "$PREV")"
docker compose logs --tail 40 engine || true
git checkout --quiet -B main "$PREV"
docker compose up -d --build
if healthy; then
  alert error "Deploy of $NEW FAILED its health check and was rolled back to $(git rev-parse --short "$PREV"). The league runs on the previous version."
else
  alert error "Deploy of $NEW failed AND the rollback is unhealthy. Look at the server now: docker compose ps / logs."
fi
exit 1
