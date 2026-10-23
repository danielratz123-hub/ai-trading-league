"""Agent Hub: one place to hand coding tasks to Claude, Codex or Gemini.

The hub runs each agent's official CLI (signed in with Joe's subscriptions) in
its own throwaway copy of the repo, runs the same checks as CI, pushes a
branch, opens a PR, waits for the automatic Claude review, feeds "Needs
fixes" back to the same agent, and stops when the PR is ready to merge or
needs Joe. It never merges by itself. See HUB.md.

It runs in its own container (docker compose profile "hub") with no access to
the league's database or .env.
"""
