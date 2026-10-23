"""Running an agent CLI (or the CI checks) as a child process."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Only these environment variables reach the agents and the checks. The
# GitHub token and Telegram settings stay with the hub.
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "USER", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "GEMINI_CLI_HOME", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "PIP_CACHE_DIR")

PIP_INSTALL = ["python", "-m", "pip", "install", "--user", "--quiet", "--disable-pip-version-check",
               "-r", "requirements.txt", "ruff==0.6.9"]
CI_COMMANDS = [
    ["python", "-m", "ruff", "check", "--select", "F", "arena", "hub"],
    ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "arena/tests", "hub/tests"],
]


@dataclass
class RunResult:
    ok: bool
    exit_code: int | None
    tail: str
    timed_out: bool = False
    stopped: bool = False


def child_env(*, drop: tuple[str, ...] = (), extra: tuple[str, ...] = ()) -> dict[str, str]:
    keep = set(PASS_ENV) | set(extra)
    env = {k: v for k, v in os.environ.items() if k in keep and k not in drop}
    env.update({"CI": "1", "TERM": "dumb", "NO_COLOR": "1", "NO_BROWSER": "true",
                "PYTHONUNBUFFERED": "1"})
    return env


def tail(path: Path, n: int = 6000) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-n:].decode("utf-8", "replace")


def run(cmd: list[str], cwd: Path, log_path: Path, timeout: int, env: dict[str, str],
        should_stop: Callable[[], bool] = lambda: False, append: bool = False) -> RunResult:
    """Run one command, output into log_path. Kills the whole process group
    on timeout or when should_stop() turns true."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab" if append else "wb") as log:
        log.write(f"$ {' '.join(c if len(c) < 120 else c[:117] + '...' for c in cmd)}\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError as e:
            log.write(f"could not start: {e}\n".encode())
            return RunResult(False, None, tail(log_path))
        deadline = time.monotonic() + timeout
        timed_out = stopped = False
        while proc.poll() is None:
            if should_stop():
                stopped = True
            elif time.monotonic() > deadline:
                timed_out = True
            if stopped or timed_out:
                _kill(proc)
                break
            time.sleep(1)
        code = proc.wait()
        if timed_out:
            log.write(f"\n[hub] stopped after {timeout // 60} min (time limit)\n".encode())
        if stopped:
            log.write(b"\n[hub] stopped (task cancelled)\n")
    return RunResult(code == 0 and not (timed_out or stopped), code, tail(log_path),
                     timed_out, stopped)


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def agent_command(agent_cfg: dict, prompt: str) -> list[str]:
    return [prompt if part == "{prompt}" else part for part in agent_cfg["cmd"]]


def run_agent(agent_cfg: dict, prompt: str, cwd: Path, log_path: Path, timeout: int,
              should_stop: Callable[[], bool] = lambda: False) -> RunResult:
    env = child_env(extra=tuple(agent_cfg.get("env_keys", ())))
    return run(agent_command(agent_cfg, prompt), cwd, log_path, timeout, env, should_stop)


def requirements_changed(cwd: Path) -> bool:
    """The image already has the league's requirements; only a task that
    changes requirements.txt needs a pip install."""
    base = Path(os.getenv("HUB_BASE_REQUIREMENTS", "/app/requirements.txt"))
    try:
        return (Path(cwd) / "requirements.txt").read_bytes() != base.read_bytes()
    except OSError:
        return True


def run_checks(cwd: Path, log_path: Path, timeout: int,
               should_stop: Callable[[], bool] = lambda: False) -> RunResult:
    """The same lint + tests CI runs. CODEX_HOME is dropped: the league's
    tests expect it unset."""
    env = child_env(drop=("CODEX_HOME",))
    started = time.monotonic()
    result = RunResult(True, 0, "")
    commands = list(CI_COMMANDS)
    if requirements_changed(cwd):
        commands.insert(0, PIP_INSTALL)
    for i, cmd in enumerate(commands):
        left = int(timeout - (time.monotonic() - started))
        if left <= 0:
            return RunResult(False, None, tail(log_path), timed_out=True)
        result = run(cmd, cwd, log_path, left, env, should_stop, append=i > 0)
        if not result.ok:
            return result
    return result
