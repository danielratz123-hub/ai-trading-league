"""Parent side of the sandbox: start child.py with limits and collect its result.

Agent code NEVER runs in this process. We only ever pass it as text to a
separate Python process that has no secrets in its environment, CPU-time,
memory and process-count caps, a wall-clock timeout, and the audit-hook
lockdown in child.py. The caps come from a Job Object on Windows (set here)
and from setrlimit on Linux (set and verified by child.py, which refuses to
run the code if any of them doesn't hold).

On Linux, where the kernel allows unprivileged user namespaces, the child
also runs in its own empty network namespace (`unshare -rn`): it has no
network interface at all, not even loopback. Where that isn't allowed (e.g.
Windows, or Docker's default seccomp profile), the other layers still apply.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .lockdown import LOCKDOWN_EXIT_CODE

CHILD = Path(__file__).with_name("child.py")
log = logging.getLogger("arena.sandbox")


@dataclass(frozen=True)
class SandboxLimits:
    cpu_seconds: int = 30
    mem_mb: int = 1536
    wall_seconds: int = 60      # also catches code that sleeps/blocks without using CPU
    max_output_chars: int = 8000


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    error: str | None = None
    weights: dict[str, float] | None = None
    series: dict[str, dict[str, float]] = field(default_factory=dict)
    refused: bool = False       # an OS limit couldn't be enforced; nothing ran


def child_env(workdir: str) -> dict[str, str]:
    """A minimal environment. Crucially, no API keys are ever copied over."""
    env = {
        "PATH": os.path.dirname(sys.executable),
        "TEMP": workdir, "TMP": workdir, "HOME": workdir,
        "PYTHONHASHSEED": "0",            # deterministic set/dict ordering
        "PYTHONDONTWRITEBYTECODE": "1",
        # One BLAS thread: predictable CPU use and reproducible float sums.
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    }
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


class PriceDataFile:
    """Writes the price frame to a temp pickle once and reuses it for every
    sandbox call in the same research session."""

    def __init__(self, frame: pd.DataFrame):
        self._dir = tempfile.TemporaryDirectory(prefix="arena_sbx_")
        self.path = os.path.join(self._dir.name, "prices.pkl")
        frame.to_pickle(self.path)
        self.workdir = self._dir.name

    def cleanup(self) -> None:
        self._dir.cleanup()


def run_code(
    mode: str,
    code: str,
    data: PriceDataFile,
    universe: dict[str, str],
    date: str,
    dates: list[str] | None = None,
    limits: SandboxLimits = SandboxLimits(),
    _simulate_limit_failure: str = "",
) -> SandboxResult:
    """mode: 'analysis' (run_python), 'strategy' (one target_weights call),
    or 'strategy_series' (target_weights for each date in `dates`)."""
    assert mode in ("analysis", "strategy", "strategy_series")
    marker = "@@RESULT_" + secrets.token_hex(8) + "@@"
    job = {
        "mode": mode, "code": code, "data_path": data.path, "universe": universe,
        "date": date, "dates": dates or [], "marker": marker,
        "cpu_seconds": limits.cpu_seconds, "mem_mb": limits.mem_mb,
        "max_output": limits.max_output_chars,
        "simulate_limit_failure": _simulate_limit_failure,
        "lockdown": lockdown_required(),
    }
    proc = subprocess.Popen(
        no_network_prefix() + [sys.executable, "-I", str(CHILD)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=data.workdir, env=child_env(data.workdir),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    job_handle = _limit_windows_process(proc, limits) if os.name == "nt" else None
    try:
        out, err = proc.communicate(json.dumps(job).encode(), timeout=limits.wall_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return SandboxResult(False, error=f"Timed out after {limits.wall_seconds}s wall-clock time.")
    finally:
        if job_handle:
            _close_handle(job_handle)

    text = out.decode("utf-8", errors="replace")
    if proc.returncode == LOCKDOWN_EXIT_CODE and marker not in text:
        return _lockdown_refused(err)
    if marker not in text:
        return SandboxResult(False, error=_explain_crash(proc.returncode, err, limits))
    payload = json.loads(text.rsplit(marker, 1)[1].strip().splitlines()[0])
    if payload.get("refused"):
        log.error("sandbox refused to run: %s", payload.get("error"))
    return SandboxResult(
        ok=payload["ok"], stdout=payload.get("stdout", ""), error=payload.get("error"),
        weights=payload.get("weights"), series=payload.get("series") or {},
        refused=bool(payload.get("refused")),
    )


def lockdown_required() -> bool:
    """Landlock + seccomp are mandatory on Linux. ARENA_SANDBOX_LOCKDOWN=off
    is for development only."""
    return (sys.platform.startswith("linux")
            and os.environ.get("ARENA_SANDBOX_LOCKDOWN", "").strip().lower() != "off")


def _lockdown_refused(stderr: bytes) -> SandboxResult:
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    reason = lines[-1][:300] if lines else "sandbox lockdown failed (no reason given)"
    log.error("sandbox refused to run agent code: %s", reason)
    # At most once per hour, so a broken sandbox doesn't spam. The raw reason
    # stays in the log only: it can hold paths.
    try:
        from arena import alerts
        alerts.send("Sandbox refused to run agent code (kernel lockdown failed).\n"
                    "Agent code is NOT run unlocked. Check the server kernel (Landlock needs "
                    "Linux 5.13+).", level="error", key="sandbox-lockdown",
                    every=timedelta(hours=1))
    except Exception:  # noqa: BLE001 - alerts must never break the caller
        log.exception("could not send the sandbox lockdown alert")
    return SandboxResult(False, error=f"Sandbox refused to run: {reason}. The code was not executed.",
                         refused=True)


@lru_cache(maxsize=1)
def no_network_prefix() -> list[str]:
    """['unshare', '-rn', '--'] if this machine can give the child its own
    empty network namespace, else []. Checked once per process."""
    if not sys.platform.startswith("linux"):
        return []
    exe = shutil.which("unshare")
    if not exe:
        return []
    try:
        ok = subprocess.run([exe, "-rn", "--", sys.executable, "-c", "pass"],
                            capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return [exe, "-rn", "--"] if ok else []


def _explain_crash(code: int, stderr: bytes, limits: SandboxLimits) -> str:
    tail = stderr.decode("utf-8", errors="replace")[-1500:]
    if "MemoryError" in tail:
        return f"Out of memory (limit {limits.mem_mb} MB)."
    # Job Objects / RLIMIT_CPU kill the process without a Python traceback.
    if code in (1816, -9, -24, 137, 152) or code < 0 or code > 255:
        return (f"Process was killed (exit code {code}): CPU limit "
                f"{limits.cpu_seconds}s or memory limit {limits.mem_mb} MB exceeded.")
    return f"Sandbox process failed (exit code {code}).\n{tail}"


# --------------------------------------------------------------------------
# Windows: Job Object with CPU-time and memory limits
# --------------------------------------------------------------------------

def _limit_windows_process(proc: subprocess.Popen, limits: SandboxLimits):
    """The child blocks reading stdin until we write the job, so the limits
    are in place before any agent code runs."""
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    LIMIT_PROCESS_TIME, LIMIT_PROCESS_MEMORY = 0x2, 0x100
    LIMIT_ACTIVE_PROCESS, LIMIT_KILL_ON_JOB_CLOSE = 0x8, 0x2000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = (
        LIMIT_PROCESS_TIME | LIMIT_PROCESS_MEMORY | LIMIT_ACTIVE_PROCESS | LIMIT_KILL_ON_JOB_CLOSE)
    info.BasicLimitInformation.PerProcessUserTimeLimit = limits.cpu_seconds * 10_000_000  # 100ns units
    # 2, not 1: a venv's python.exe is a launcher that starts the real
    # interpreter as a second process. That interpreter can't start a third.
    info.BasicLimitInformation.ActiveProcessLimit = 2
    info.ProcessMemoryLimit = limits.mem_mb * 1024 * 1024
    ok = k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
    ok = ok and h and k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(h))
    if h:
        k32.CloseHandle(wintypes.HANDLE(h))
    if not ok:
        proc.kill()
        raise RuntimeError(f"Could not apply sandbox limits (WinError {ctypes.get_last_error()})")
    return job


def _close_handle(handle) -> None:
    import ctypes
    from ctypes import wintypes
    ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(handle))
