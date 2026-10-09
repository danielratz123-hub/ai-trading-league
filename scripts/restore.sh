#!/bin/sh
# Restore the league database from a backup (run on the server, in the
# project folder, as a user in the docker group).
#
#   ./scripts/restore.sh latest            newest backup (remote, else local)
#   ./scripts/restore.sh arena/daily/arena-2026-10-09.db.gz
#   ./scripts/restore.sh local/arena-2026-10-09.db.gz
#   ./scripts/restore.sh latest --test     restore into a COPY and check it;
#                                          the live league is not touched
#
# List backups: docker compose exec engine python -m arena.main backups
#
# What a real restore does:
#   1. stops engine, api and setup (nothing may write while the file changes)
#   2. downloads the backup, decompresses it and runs an integrity check
#   3. renames the current arena.db (and -wal/-shm) to *.before-restore-<time>
#   4. puts the backup in place and starts everything again
#   5. prints the league status
# Undo: stop the services and rename the *.before-restore-* files back.
set -eu
cd "$(dirname "$0")/.."

NAME="${1:-}"
MODE="${2:-}"
if [ -z "$NAME" ]; then
  sed -n '2,20p' "$0"
  exit 2
fi

run() {   # a one-off engine container with the data volume, no other services
  docker compose run --rm --no-deps -T engine "$@"
}

if [ "$MODE" = "--test" ]; then
  echo "== Test restore of '$NAME' into a copy (the live database is not touched)"
  run python -m arena.main restore-backup "$NAME" --to /app/data/restore-test.db
  run python -m arena.main status --db /app/data/restore-test.db
  run sh -c 'rm -f /app/data/restore-test.db /app/data/restore-test.db-wal /app/data/restore-test.db-shm'
  echo "== Test restore OK"
  exit 0
fi

printf "This replaces the live league database with backup '%s'.\nType 'restore' to continue: " "$NAME"
read -r answer
[ "$answer" = "restore" ] || { echo "Cancelled."; exit 1; }

echo "== Stopping the app"
docker compose stop engine api setup

echo "== Fetching and checking the backup"
if ! run python -m arena.main restore-backup "$NAME" --to /app/data/arena.db.restored; then
  echo "Restore failed; starting the app again with the current database."
  docker compose up -d
  exit 1
fi

echo "== Swapping the database files"
run sh -c '
  set -eu
  cd /app/data
  ts=$(date +%Y%m%d-%H%M%S)
  for f in arena.db arena.db-wal arena.db-shm; do
    if [ -e "$f" ]; then mv "$f" "$f.before-restore-$ts"; fi
  done
  mv arena.db.restored arena.db
  echo "previous database kept as arena.db.before-restore-$ts"
'

echo "== Starting the app"
docker compose up -d
sleep 5
docker compose exec -T engine python -m arena.main status
echo "== Restore done. Check the scoreboard on the dashboard."
