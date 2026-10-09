"""Runs ONE piece of agent-written code inside a locked-down process.

Started by runner.py as:  python -I child.py   (job JSON on stdin)

This file must stay self-contained: it must never import the `arena`
package, because arena.config loads .env (API keys) into the environment.

Lockdown layers, in order:
  1. The parent gives us an environment with no secrets, a temp working
     dir, and OS limits (CPU time, memory).
  2. We load the price data and import pandas/numpy FIRST, while file access
     is still allowed.
  3. We install an audit hook. Audit hooks are checked inside CPython's C
     code for every file open, socket, subprocess, etc., and can't be
     removed once installed. After this point the process can't open files
     (except reading Python's own library code), use the network, or start
     programs.
  4. Agent code runs with a restricted __import__ that only allows
     pandas, numpy, math and statistics.
"""
import builtins
import io
import json
import math
import os
import sys
import traceback

ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics"}

# Audit events that are always refused after lockdown (prefix match).
BLOCKED_EVENT_PREFIXES = (
    "socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.kill", "os.startfile", "os.remove", "os.unlink", "os.rename",
    "os.rmdir", "os.mkdir", "os.chdir", "os.listdir", "os.scandir", "os.chmod",
    "os.symlink", "os.link", "os.truncate", "os.putenv", "os.unsetenv",
    "shutil.", "ctypes.", "winreg.", "urllib.", "http.", "ftplib.", "smtplib.",
    "webbrowser.", "glob.", "pathlib.", "msvcrt.", "_winapi.", "sqlite3.",
)
BLOCKED_MODULES = {
    "ctypes", "_ctypes", "socket", "_socket", "ssl", "subprocess", "_posixsubprocess",
    "multiprocessing", "_multiprocessing", "urllib", "http", "requests", "asyncio",
    "winreg", "_winapi", "shutil", "pickle", "_pickle", "marshal", "importlib",
    "sqlite3", "_sqlite3", "pty", "signal", "threading",
}


def _apply_unix_limits(cpu_seconds: int, mem_mb: int) -> None:
    try:
        import resource
    except ImportError:  # Windows: parent uses a Job Object instead
        return
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    mem = mem_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))


def _make_audit_hook(read_roots: list[str]):
    roots = [os.path.normcase(os.path.abspath(r)) for r in read_roots]

    def hook(event, args):
        if event == "open":
            path, mode = args[0], args[1] if len(args) > 1 else "r"
            mode = mode or "r"
            if isinstance(path, str) and not any(c in str(mode) for c in "wax+"):
                p = os.path.normcase(os.path.abspath(path))
                # Reading Python's own library files is needed for pandas'
                # lazy imports; those files are public code, not secrets.
                if any(p.startswith(r + os.sep) for r in roots):
                    return
            raise PermissionError(f"sandbox: file access is not allowed ({path!r})")
        if event == "import":
            top = str(args[0]).split(".")[0]
            if top in BLOCKED_MODULES:
                raise ImportError(f"sandbox: module {top!r} is not allowed")
            return
        if event.startswith(BLOCKED_EVENT_PREFIXES):
            raise PermissionError(f"sandbox: operation {event!r} is not allowed")

    return hook


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0 or name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError(f"import of {name!r} is not allowed; "
                          f"allowed: {', '.join(sorted(ALLOWED_IMPORTS))}")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _safe_builtins() -> dict:
    b = dict(vars(builtins))
    for name in ("open", "input", "breakpoint", "exit", "quit", "help", "compile",
                 "eval", "exec", "globals", "vars", "memoryview", "__loader__", "__spec__"):
        b.pop(name, None)
    b["__import__"] = _restricted_import
    return b


def _check_weights(w, universe) -> dict:
    if not isinstance(w, dict):
        raise TypeError(f"target_weights must return a dict, got {type(w).__name__}")
    out = {}
    for k, v in w.items():
        if not isinstance(k, str):
            raise TypeError(f"weight key {k!r} is not a ticker string")
        try:
            f = float(v)
        except Exception:
            raise TypeError(f"weight for {k} is not a number: {v!r}") from None
        if not math.isfinite(f):
            raise ValueError(f"weight for {k} is not finite: {v!r}")
        if k not in universe:
            raise ValueError(f"{k} is not in the tradable universe")
        if f != 0:
            out[k] = f
    gross = sum(abs(x) for x in out.values())
    if gross > 1 + 1e-6:
        raise ValueError(f"sum of |weights| is {gross:.4f}; it must be <= 1")
    return out


def main() -> None:
    real_stdout = sys.stdout
    job = json.loads(sys.stdin.read())
    _apply_unix_limits(job["cpu_seconds"], job["mem_mb"])

    # --- load data while file access is still allowed -------------------
    import numpy as np  # noqa: F401  (pre-import so lazy loads are rare)
    import pandas as pd
    import statistics  # noqa: F401

    prices = pd.read_pickle(job["data_path"])
    universe = dict(job["universe"])
    read_roots = sorted({sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix})

    # --- lock down -------------------------------------------------------
    sys.addaudithook(_make_audit_hook(read_roots))

    captured = io.StringIO()
    result = {"ok": True, "stdout": "", "error": None}
    namespace = {"__builtins__": _safe_builtins(), "__name__": "agent_code"}
    if job["mode"] == "analysis":
        namespace.update(prices=prices, universe=universe, decision_date=job["date"])
    sys.stdout = captured
    try:
        exec(job["code"], namespace)
        if job["mode"] in ("strategy", "strategy_series"):
            fn = namespace.get("target_weights")
            if not callable(fn):
                raise NameError("code must define target_weights(prices, date)")
            dates = [job["date"]] if job["mode"] == "strategy" else job["dates"]
            series = {}
            for d in dates:
                # Each call only sees bars up to its own decision date.
                view = prices.loc[:d].copy()
                series[d] = _check_weights(fn(view, d), universe)
            if job["mode"] == "strategy":
                result["weights"] = series[job["date"]]
            else:
                result["series"] = series
    except BaseException as exc:  # noqa: BLE001 - report everything to the agent
        # Keep only the agent's own frames ("<string>") and library frames
        # below them; the sandbox's frames are noise to the agent.
        frames = [f for f in traceback.extract_tb(exc.__traceback__)
                  if os.path.basename(f.filename) != "child.py"]
        text = "".join(traceback.format_list(frames))
        text += "".join(traceback.format_exception_only(type(exc), exc))
        result.update(ok=False, error=text[-3000:])
    finally:
        sys.stdout = real_stdout
    out = captured.getvalue()
    limit = job["max_output"]
    if len(out) > limit:
        out = out[:limit] + f"\n... [output truncated: {len(out)} chars, showing first {limit}]"
    result["stdout"] = out
    real_stdout.write("\n" + job["marker"] + json.dumps(result) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
