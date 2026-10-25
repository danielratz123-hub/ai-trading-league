"""Exercise the deploy ancestry lock in a disposable repo, without a network."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("target", ["origin/main", "ancestor", "outside", "missing", "fetch-failed", "invalid"])
def test_deploy_only_accepts_main_history(tmp_path, target):
    real_git = shutil.which("git")
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output([real_git, *args], cwd=repo, text=True).strip()
    git("init", "-q")
    def commit(message):
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-qm", message)
        return git("rev-parse", "HEAD")
    ancestor = commit("ancestor")
    main = commit("main")
    git("update-ref", "refs/remotes/origin/main", main)
    outside = commit("outside-main")
    scripts = repo / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"
    shutil.copy(source, scripts / "deploy.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    trace = tmp_path / "trace"
    # Fetch is stubbed; rev-parse and merge-base use real local git objects.
    (binaries / "git").write_text('''#!/bin/sh
printf 'git %s\\n' "$*" >> "$TRACE"
case "$1" in
fetch) [ "$FETCH_FAIL" != 1 ]; exit $? ;;
checkout) exit 88 ;;
esac
exec "$REAL_GIT" "$@"
''')
    (binaries / "docker").write_text('''#!/bin/sh
printf 'docker %s\\n' "$*" >> "$TRACE"
# busy() receives zero; alert() also succeeds without sending anything.
printf '0\\n'
''')
    (binaries / "date").write_text('''#!/bin/sh
case "$1" in +%H%M) echo 1200 ;; +%u) echo 7 ;; esac
''')
    for file in binaries.iterdir():
        file.chmod(0o755)
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
               REAL_GIT=real_git, TRACE=str(trace), FETCH_FAIL="1" if target == "fetch-failed" else "0")
    argument = {"ancestor": ancestor, "outside": outside, "missing": "abcdef0",
                "fetch-failed": "origin/main", "invalid": "branch-name"}.get(target, target)
    result = subprocess.run(["sh", str(scripts / "deploy.sh"), argument],
                            env=env, capture_output=True, text=True)
    calls = trace.read_text()
    if target != "invalid":
        assert calls.startswith("git fetch")
    if target in ("origin/main", "ancestor"):
        assert result.returncode == 88  # reached checkout, which is safely stubbed
        assert f"git checkout --quiet -B main {main if target == 'origin/main' else ancestor}" in calls
    else:
        assert result.returncode == (2 if target == "invalid" else 1)
        assert "Deploy refused" in result.stdout
        assert "alerts.send" in calls and "error" in calls
        assert "git checkout" not in calls and "compose up" not in calls
    assert git("rev-parse", "HEAD") == outside


def test_ignore_secret_and_backup_paths(tmp_path):
    source = Path(__file__).resolve().parents[2] / ".gitignore"
    shutil.copy(source, tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    paths = [".env", ".env.production", "nested/.env.local", "hub.env", "service.env.old",
             "nested/arena.db", "nested/arena.db-wal", "nested/cache.sqlite", "test.sqlite3",
             "private.key", "private.pem", "credentials.json", "service-token.json",
             "service_account.json", "backups/archive", "snapshot.bak", "snapshot.tar.gz"]
    # Check names only; never create any secret/configuration/database files.
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=tmp_path,
                            input="\n".join(paths) + "\n", text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == paths
    examples = subprocess.run(["git", "check-ignore", "--stdin"], cwd=tmp_path,
                              input=".env.example\nhub.env.example\n", text=True, capture_output=True)
    assert examples.returncode == 1 and not examples.stdout
