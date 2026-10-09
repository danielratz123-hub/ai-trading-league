"""The setup / control panel API (used by web/setup.html).

Unlike the rest of the API this one can change things: save keys, run
commands, start the scheduler. So it is locked to *your own computer*:

  * the request must come from 127.0.0.1 / ::1 (not from the network);
  * the Host header must be localhost (blocks "DNS rebinding" tricks);
  * a browser Origin, if sent, must be localhost too, and every POST must
    carry an X-Arena-Control header. Other websites can't add that header
    without a CORS preflight, which our CORS settings refuse, so a page
    you visit elsewhere can't press these buttons for you.
  * ARENA_DISABLE_CONTROL=1 turns it off completely (the Docker setup does).

Long jobs (downloading data, kickoff, a practice week) run one at a time
as `python -m arena.main ...` child processes; their output is streamed to
the page through /api/control/task.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import envfile, league, ledger
from ..agents.cli import CLI_ADAPTERS, resolve_cli
from ..config import PROJECT_ROOT, SETTINGS
from ..db import connect
from ..prices import ET, latest_price_date

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def db_path() -> str:
    return os.getenv("ARENA_DB", str(SETTINGS.db_path))


def control_enabled() -> bool:
    return os.getenv("ARENA_DISABLE_CONTROL", "0") != "1"


def require_local(request: Request) -> None:
    if not control_enabled():
        raise HTTPException(404, "The control panel is disabled on this server.")
    client = request.client.host if request.client else ""
    host = urlparse(f"//{request.headers.get('host', '')}").hostname or ""
    origin = request.headers.get("origin")
    if client not in LOCAL_HOSTS or host not in LOCAL_HOSTS:
        raise HTTPException(403, "The control panel only works on the computer running the league.")
    if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
        raise HTTPException(403, "Requests from other websites are not allowed.")
    if request.method != "GET" and request.headers.get("x-arena-control") != "1":
        raise HTTPException(403, "Missing X-Arena-Control header.")


# --------------------------------------------------------------------------
# Background tasks (one at a time)
# --------------------------------------------------------------------------

def _cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "arena.main", *args, "--db", db_path()]


class TaskRunner:
    """Runs a named list of commands in a background thread and keeps the
    last lines of output for the page to show."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name: str | None = None
        self.status = "idle"            # idle | running | done | failed
        self.started: str | None = None
        self.lines: deque[str] = deque(maxlen=400)
        self.proc: subprocess.Popen | None = None

    def busy(self) -> bool:
        return self.status == "running"

    def start(self, name: str, steps: list[list[str]]) -> None:
        with self.lock:
            if self.busy():
                raise HTTPException(409, f"'{self.name}' is still running. Wait for it to finish.")
            self.name, self.status, self.started = name, "running", datetime.now(ET).strftime("%H:%M:%S")
            self.lines.clear()
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()

    def _run(self, steps: list[list[str]]) -> None:
        ok = True
        for argv in steps:
            self.lines.append("$ " + " ".join(Path(a).name if i == 0 else a for i, a in enumerate(argv)))
            try:
                self.proc = subprocess.Popen(
                    argv, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                    env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                for line in self.proc.stdout:
                    self.lines.append(line.rstrip())
                code = self.proc.wait()
            except OSError as e:
                self.lines.append(f"Could not start: {e}")
                code = 1
            if code != 0:
                self.lines.append(f"(stopped: exit code {code})")
                ok = False
                break
        self.proc = None
        self.status = "done" if ok else "failed"

    def state(self) -> dict:
        return {"name": self.name, "status": self.status, "started": self.started, "log": list(self.lines)}


# --------------------------------------------------------------------------
# The scheduler as a child process of this server
# --------------------------------------------------------------------------

class SchedulerProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        log_dir = Path(db_path()).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out = open(log_dir / "scheduler-console.log", "a", encoding="utf-8")
        if os.name == "nt":
            self.proc = subprocess.Popen(_cmd("scheduler"), cwd=PROJECT_ROOT, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            self._job = _kill_with_this_process(self.proc)
        else:
            # Its own process group (so stop() also ends the job it's running),
            # and ARENA_PARENT_PID so it exits if this app dies (scheduler.py).
            self.proc = subprocess.Popen(_cmd("scheduler"), cwd=PROJECT_ROOT, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True,
                                         env={**os.environ, "ARENA_PARENT_PID": str(os.getpid())})

    def stop(self) -> None:
        if not self.running():
            return
        if os.name == "nt":   # also stop jobs it started
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _kill_with_this_process(proc: subprocess.Popen):
    """Put a child in a Windows Job Object that kills it when this process
    ends. Without it, closing the start.cmd window leaves the scheduler
    running in the background, and the next start adds a second one."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    h = k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)   # SET_QUOTA | TERMINATE
    if h:
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(h))
        k32.CloseHandle(wintypes.HANDLE(h))
    return job   # the handle stays open while this process lives; Windows closes it on exit


TASKS = TaskRunner()
SCHEDULER = SchedulerProcess()
_AGENT_CHECK: dict = {}


def server_mode() -> bool:
    """On the VPS the engine container runs the scheduler; this page only shows it."""
    return os.getenv("ARENA_SERVER", "0") == "1"


def autostart_scheduler() -> None:
    """Called when the server starts: resume the scheduler if you asked for it."""
    if server_mode() or not control_enabled() or os.getenv("AUTOSTART_SCHEDULER", "0") != "1":
        return
    if Path(db_path()).exists():
        conn = connect(db_path())
        started = bool(ledger.agent_ids(conn))
        conn.close()
        if started:
            SCHEDULER.start()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter(prefix="/api/control", dependencies=[Depends(require_local)])


def _league_state() -> dict:
    if not Path(db_path()).exists():
        return {"started": False, "paused": False, "prices_up_to": None, "tickers": 0, "calendar_until": None,
                "latest_week": None, "recent_jobs": []}
    conn = connect(db_path())
    try:
        week = conn.execute("SELECT * FROM weeks ORDER BY week_no DESC LIMIT 1").fetchone()
        jobs = [dict(r) for r in conn.execute(
            "SELECT job, key, started_at, exit_code FROM job_runs ORDER BY id DESC LIMIT 8")]
        return {
            "started": bool(ledger.agent_ids(conn)),
            "paused": league.is_paused(conn),
            "prices_up_to": latest_price_date(conn),
            "tickers": conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0],
            "calendar_until": conn.execute("SELECT MAX(date) FROM calendar").fetchone()[0],
            "latest_week": dict(week) if week else None,
            "recent_jobs": jobs,
        }
    finally:
        conn.close()


@router.get("/state")
def state():
    envfile.ensure_env_file()
    now = datetime.now(ET)
    return {
        "env": envfile.describe(),
        "league": _league_state(),
        "task": TASKS.state(),
        "scheduler": _scheduler_state(),
        "agents": _AGENT_CHECK,
        "now_et": now.strftime("%a %Y-%m-%d %H:%M"),
        "weekend": now.weekday() >= 5,
        "can_open_windows": os.name == "nt",
    }


@router.post("/env")
def save_env(updates: dict):
    envfile.ensure_env_file()
    try:
        envfile.set_values({k: v for k, v in updates.items() if v is not None})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"env": envfile.describe()}


@router.post("/test-alpaca")
def test_alpaca():
    try:
        from alpaca.trading.client import TradingClient
        key, secret = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return {"ok": False, "message": "Save both Alpaca keys first."}
        acct = TradingClient(key, secret, paper=True).get_account()
        return {"ok": True, "message": f"Connected to your Alpaca paper account (status {acct.status})."}
    except Exception as e:  # noqa: BLE001 - show any connection problem in plain words
        msg = str(e)
        if "401" in msg or "forbidden" in msg.lower() or "unauthorized" in msg.lower():
            msg = "Alpaca rejected these keys. Copy them again from the Paper Trading dashboard."
        return {"ok": False, "message": msg[:300]}


@router.post("/test-mirror")
def test_mirror():
    from .. import mirror
    agents = list(SETTINGS.agents)
    if mirror.mode() in ("leader", "combined"):
        agents = agents[:1]       # one account; any agent id tests the main keys
    return mirror.check_accounts(agents)


@router.post("/agents/check")
def check_agents():
    out = {}
    for agent_id, cls in CLI_ADAPTERS.items():
        agent = cls(agent_id, SETTINGS.models.get(agent_id, ""))
        inst_ok, inst = agent.check_install()
        login_ok, login = agent.check_login() if inst_ok else (False, "not installed")
        out[agent_id] = {"installed": inst_ok, "version": inst, "signed_in": login_ok, "login": login}
    _AGENT_CHECK.clear()
    _AGENT_CHECK.update(out)
    return out


@router.post("/agents/{agent_id}/login")
def open_login(agent_id: str):
    cls = CLI_ADAPTERS.get(agent_id)
    if cls is None:
        raise HTTPException(404, "Unknown agent.")
    args = {"claude": ["auth", "login"], "gpt": ["login"]}[agent_id]
    try:
        argv = resolve_cli(cls.CLI_NAME, cls.NPM_JS) + args
    except FileNotFoundError:
        raise HTTPException(400, f"Install {cls.CLI_NAME} first.") from None
    if os.name != "nt":
        return {"opened": False, "message": f"Run this in a terminal: {cls.CLI_NAME} {' '.join(args)}"}
    # A visible window, because signing in may ask you something.
    subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=PROJECT_ROOT)
    return {"opened": True, "message": "A sign-in window opened. Finish there, then press Check."}


def _npm_install_steps() -> list[list[str]]:
    npm = shutil.which("npm")
    if not npm:
        raise HTTPException(400, "Node.js (npm) isn't installed. Get the LTS version from nodejs.org, then retry.")
    return [[npm, "install", "-g", "@anthropic-ai/claude-code", "@openai/codex"]]


@router.post("/run/{action}")
def run(action: str, body: dict | None = None):
    body = body or {}
    if action == "install-agents":
        steps = _npm_install_steps()
    elif action == "test-agents":
        steps = [_cmd("check-agents", "--probe")]
    elif action == "download-data":
        steps = [_cmd("update-calendar"), _cmd("update-prices")]
    elif action == "mirror-now":
        steps = [_cmd("mirror", "--apply")]
    elif action == "kickoff":
        steps = [_cmd("kickoff")]
    elif action == "practice-week":
        start = str(body.get("start", ""))
        try:
            datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Pick a start date.") from None
        weeks = int(body.get("weeks", 1))
        if not 1 <= weeks <= 4:
            raise HTTPException(400, "Practice runs are limited to 1-4 weeks here.")
        steps = [_cmd("simulate", "--start", start, "--weeks", str(weeks), "--yes")]
    else:
        raise HTTPException(404, f"Unknown action {action!r}.")
    TASKS.start(action, steps)
    return TASKS.state()


@router.get("/models")
def models():
    from ..agents.models import catalog
    return catalog()


@router.get("/task")
def task():
    return TASKS.state()


def _scheduler_state() -> dict:
    if server_mode():
        from ..health import HEARTBEAT_STALE, heartbeat_age
        age = heartbeat_age(db_path())
        return {"running": age is not None and age < HEARTBEAT_STALE, "managed": "engine"}
    return {"running": SCHEDULER.running()}


@router.post("/scheduler/{what}")
def scheduler(what: str):
    if server_mode():
        raise HTTPException(409, "On the server the engine container runs the scheduler: "
                                 "docker compose restart engine / docker compose stop engine.")
    if what == "start":
        SCHEDULER.start()
    elif what == "stop":
        SCHEDULER.stop()
    else:
        raise HTTPException(404)
    time.sleep(0.3)
    return {"running": SCHEDULER.running()}
