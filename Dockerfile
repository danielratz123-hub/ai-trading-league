# Engine + API image. The same image runs the scheduler (engine) and the
# dashboard API; docker-compose.yml picks the command.
FROM python:3.12-slim-bookworm

# Node.js (for the Claude Code and Codex CLIs), copied from the official image.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update && apt-get install -y --no-install-recommends tzdata util-linux ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code @openai/codex \
 && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY arena ./arena
COPY scripts ./scripts

# Run as an unprivileged user; data/ and the CLIs' login folders are volumes.
RUN useradd --create-home --uid 1000 arena \
 && mkdir -p /app/data /home/arena/.claude /home/arena/.codex \
 && chown -R arena:arena /app/data /home/arena
USER arena
ENV PYTHONUNBUFFERED=1 TZ=America/New_York

CMD ["python", "-m", "arena.main", "status"]
