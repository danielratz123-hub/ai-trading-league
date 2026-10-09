# One image for every service (engine = scheduler, api = dashboard + API).
# docker-compose.yml picks the command. Versions are pinned so a rebuild
# gives the same program: change them on purpose, then run the tests.
FROM node:22.16.0-bookworm-slim AS node

FROM python:3.14.3-slim-bookworm

# Node.js for the two agent CLIs, copied from the official Node image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

# tzdata: market times in US/Eastern. util-linux: `unshare` (the sandbox's
# no-network layer, when the kernel allows it). bubblewrap: Codex's Linux
# sandbox. sqlite3: lets restore.sh check a backup before using it.
# rclone: backups to Google Drive (or any rclone remote).
ARG CLAUDE_CODE_VERSION=2.1.293
ARG CODEX_VERSION=0.161.0
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update \
 && apt-get install -y --no-install-recommends tzdata util-linux ca-certificates bubblewrap sqlite3 rclone \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY arena ./arena
COPY scripts ./scripts

# Run as an unprivileged user. APP_UID should be your own user id on the
# server (`id -u`), so the setup page can edit the bind-mounted .env while it
# stays private to you (chmod 600). data/ and the CLIs' login folders are
# named volumes, so they survive rebuilds. CLAUDE_CONFIG_DIR keeps ALL of
# Claude Code's files (including what it normally puts in ~/.claude.json)
# inside the claude-home volume.
ARG APP_UID=1000
RUN useradd --create-home --uid ${APP_UID} arena \
 && mkdir -p /app/data /home/arena/.claude /home/arena/.codex /home/arena/.config/rclone \
 && chown -R arena:arena /app/data /home/arena
USER arena
ENV PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    ARENA_DB=/app/data/arena.db \
    CLAUDE_CONFIG_DIR=/home/arena/.claude \
    CODEX_HOME=/home/arena/.codex

CMD ["python", "-m", "arena.main", "status"]
