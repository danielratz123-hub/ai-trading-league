#!/bin/sh
# Linux/macOS equivalent of start.cmd, for running WITHOUT Docker
# (e.g. testing on a Linux laptop). On the VPS use Docker instead (DEPLOY.md).
# Starts the app; with AUTOSTART_SCHEDULER=1 in .env it also starts the
# scheduler, which stops when the app stops (Ctrl+C).
set -eu
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "First start: setting things up. This takes a few minutes..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python -m arena.main app
