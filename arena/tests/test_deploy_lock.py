"""Tests for the deploy lock (scripts/deploy.sh) and .gitignore."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = ROOT / "scripts" / "deploy.sh"

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def _run_deploy_check(repo: Path, target: str | None = None, bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DEPLOY_REPO": str(repo),
        "DEPLOY_CHECK_ONLY": "1",
    }
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    cmd = ["sh", str(DEPLOY_SH)]
    if target is not None:
        cmd.append(target)
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True)


@requires_git
def test_deploy_lock_allows_main_and_refuses_non_main(tmp_path: Path):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    alert_log = tmp_path / "alerts.log"

    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{alert_log}"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC)

    _git(tmp_path, "init", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "checkout", "-b", "main")
    _git(repo, "remote", "add", "origin", str(origin))

    (repo / "file.txt").write_text("main v1\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "main commit")
    main_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "checkout", "-b", "side")
    (repo / "file.txt").write_text("side branch\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "side commit")
    side_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    # Main commit (full SHA and short SHA) and origin/main pass
    res_main = _run_deploy_check(repo, main_commit, bin_dir)
    assert res_main.returncode == 0, res_main.stdout + res_main.stderr

    res_short = _run_deploy_check(repo, main_commit[:7], bin_dir)
    assert res_short.returncode == 0, res_short.stdout + res_short.stderr

    res_origin_main = _run_deploy_check(repo, "origin/main", bin_dir)
    assert res_origin_main.returncode == 0, res_origin_main.stdout + res_origin_main.stderr

    res_default = _run_deploy_check(repo, None, bin_dir)
    assert res_default.returncode == 0, res_default.stdout + res_default.stderr

    # Side-branch commit is refused with exit 3, clear message, and Telegram alert
    res_side = _run_deploy_check(repo, side_commit, bin_dir)
    assert res_side.returncode == 3
    assert f"Refusing to deploy {side_commit}: it is not on origin/main" in res_side.stdout
    assert alert_log.exists()
    assert f"error Refusing to deploy {side_commit}: it is not on origin/main" in alert_log.read_text(encoding="utf-8")

    # Unknown commit id is refused with exit 3
    unknown_id = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    res_unknown = _run_deploy_check(repo, unknown_id, bin_dir)
    assert res_unknown.returncode == 3
    assert f"Refusing to deploy {unknown_id}: it is not on origin/main" in res_unknown.stdout

    # Non-hex branch/ref name is refused by the case check with exit 2
    res_bad_ref = _run_deploy_check(repo, "side", bin_dir)
    assert res_bad_ref.returncode == 2
    assert "Not a commit id: side" in res_bad_ref.stdout

    # Failed git fetch origin main is refused with exit 3
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    res_bad_fetch = _run_deploy_check(repo, main_commit, bin_dir)
    assert res_bad_fetch.returncode == 3
    assert f"Refusing to deploy {main_commit}" in res_bad_fetch.stdout


@requires_git
def test_gitignore_patterns_and_tracked_files():
    ignored_samples = [
        ".env",
        ".env.production",
        ".env.local",
        "data/arena.db",
        "arena.db",
        "x/y.sqlite",
        "a/b/c.sqlite3",
        "data/arena.db-wal",
        "data/arena.db-shm",
        "server.pem",
        "certs/private.key",
        "bundle.p12",
        "cert.pfx",
        "id_rsa",
        "id_rsa.pub",
        "keys/id_ed25519",
        "credentials.json",
        "credentials_prod.json",
        "token.json",
        "sub/token_oauth.json",
        "client_secret.json",
        "client_secret_google.json",
        "service-account.json",
        "service-account-key.json",
        "backups/arena.sql",
        "arena.bak",
        "dump.sql.gz",
        "backup.tar.gz",
    ]
    for sample in ignored_samples:
        res = subprocess.run(
            ["git", "check-ignore", "-q", sample],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"Expected {sample!r} to be ignored by .gitignore"

    # .env.example must NOT be ignored (even with --no-index)
    for args in (["git", "check-ignore", "-q", ".env.example"], ["git", "check-ignore", "--no-index", "-q", ".env.example"]):
        res_example = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert res_example.returncode == 1, ".env.example should not be ignored"

    # No already-tracked file is ignored
    res_tracked = subprocess.run(
        ["git", "ls-files", "-c", "-i", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert res_tracked.stdout.strip() == "", f"Tracked files matched .gitignore: {res_tracked.stdout}"


@requires_git
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
        assert result.returncode == (2 if target == "invalid" else 3)
        assert ("Not a commit id" if target == "invalid" else "Refusing to deploy") in result.stdout
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
