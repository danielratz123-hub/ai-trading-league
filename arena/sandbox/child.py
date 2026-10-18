"""Runs ONE piece of agent-written code inside a locked-down process.

Started by runner.py as:  python -I child.py   (job JSON on stdin)

This file must stay self-contained: it must never import the `arena`
package, because arena.config loads .env (API keys) into the environment.

Lockdown layers, in order:
  1. The parent gives us an environment with no secrets and a temp working
     dir. OS limits on CPU time, memory and new processes come from a Job
     Object (Windows, set by the parent) or setrlimit (Linux, set here).
     If any limit can't be enforced, nothing runs.
  2. We load the price data and import pandas/numpy FIRST, while file access
     is still allowed.
  2b. Linux only: kernel lockdown (_kernel_lockdown). Landlock lets the
     process read Python's own library folders and nothing else (no /proc,
     no /app/data, no CLI logins), write nowhere, and (ABI 4+) open no TCP
     connection; a seccomp filter refuses socket(), ptrace and friends. Both
     are checked after they're applied; if either is missing, nothing runs.
     This is what holds even if agent code gets past the audit hook below,
     which is a Python-level check, not a security boundary.
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


class LimitsUnavailable(RuntimeError):
    """A required OS limit could not be put in place: refuse to run."""


def _apply_limits(cpu_seconds: int, mem_mb: int, simulate_failure: str = "") -> None:
    """CPU time, address space and "no new processes" via setrlimit.

    On Windows the parent already put us in a Job Object with the same three
    limits, so there is nothing to do here. Everywhere else, each limit is
    set AND then checked; if any of them didn't take, we refuse to run the
    agent's code at all rather than run it with fewer protections.
    """
    if os.name == "nt":
        if simulate_failure:        # tests only: exercise the refusal path
            raise LimitsUnavailable(f"{simulate_failure} could not be set (simulated)")
        return
    try:
        import resource
    except ImportError:
        raise LimitsUnavailable("the resource module is missing") from None
    mem = mem_mb * 1024 * 1024
    wanted = {
        "RLIMIT_CPU": (cpu_seconds, cpu_seconds + 1),
        "RLIMIT_AS": (mem, mem),
        # Count of processes for this user: 1 (or anything already reached)
        # means fork()/clone() fail. Existing processes are not affected.
        "RLIMIT_NPROC": (1, 1),
    }
    for name, (soft, hard) in wanted.items():
        res = getattr(resource, name, None)
        try:
            if res is None or name == simulate_failure:
                raise OSError("not supported on this system")
            resource.setrlimit(res, (soft, hard))
            if resource.getrlimit(res) != (soft, hard):
                raise OSError(f"read back {resource.getrlimit(res)}")
        except (OSError, ValueError) as e:
            raise LimitsUnavailable(f"{name} could not be set ({e})") from None
    # RLIMIT_NPROC is ignored for privileged processes (real root with
    # CAP_SYS_RESOURCE / CAP_SYS_ADMIN). Prove it works: a fork must fail.
    try:
        pid = os.fork()
    except OSError:
        return                      # good: no new processes possible
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    raise LimitsUnavailable("RLIMIT_NPROC is not enforced (is the sandbox running as root?)")


# --------------------------------------------------------------------------
# Linux kernel lockdown: Landlock (files, TCP) + seccomp (sockets, ptrace)
# --------------------------------------------------------------------------

# Landlock filesystem rights. Reading = EXECUTE | READ_FILE | READ_DIR.
LL_READ = (1 << 0) | (1 << 2) | (1 << 3)
LL_NET_BIND_TCP, LL_NET_CONNECT_TCP = 1 << 0, 1 << 1
LL_SCOPE_ABSTRACT_UNIX, LL_SCOPE_SIGNAL = 1 << 0, 1 << 1
SYS_LL_CREATE, SYS_LL_ADD_RULE, SYS_LL_RESTRICT = 444, 445, 446   # x86_64 and aarch64

# seccomp: syscalls refused with EPERM. Everything else is allowed.
_SECCOMP_ARCH = {   # machine: (AUDIT_ARCH, {name: number})
    "x86_64": (0xC000003E, {"socket": 41, "socketpair": 53, "ptrace": 101,
                            "process_vm_readv": 310, "process_vm_writev": 311,
                            "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427}),
    "aarch64": (0xC00000B7, {"socket": 198, "socketpair": 199, "ptrace": 117,
                             "process_vm_readv": 270, "process_vm_writev": 271,
                             "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427}),
}
# Shared libraries can be loaded lazily from here (public code, no secrets).
_SYSTEM_LIB_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")


def _landlock_fs_rights(abi: int) -> int:
    rights = (1 << 13) - 1          # ABI 1: execute .. make_sym
    if abi >= 2:
        rights |= 1 << 13           # refer
    if abi >= 3:
        rights |= 1 << 14           # truncate
    if abi >= 5:
        rights |= 1 << 15           # ioctl_dev
    return rights


def _landlock(read_dirs: list[str]) -> int:
    """Read-only access beneath read_dirs, nothing else; no TCP on ABI 4+.
    Returns the ABI version used."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(SYS_LL_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        raise LimitsUnavailable("Landlock is not available on this kernel")

    class Attr(ctypes.Structure):
        _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    attr = Attr(_landlock_fs_rights(abi),
                (LL_NET_BIND_TCP | LL_NET_CONNECT_TCP) if abi >= 4 else 0,
                (LL_SCOPE_ABSTRACT_UNIX | LL_SCOPE_SIGNAL) if abi >= 6 else 0)
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    ruleset = libc.syscall(SYS_LL_CREATE, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if ruleset < 0:
        raise LimitsUnavailable(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for d in read_dirs:
            if not os.path.isdir(d):
                continue
            fd = os.open(d, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathBeneath(LL_READ, fd)
                if libc.syscall(SYS_LL_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(1),
                                ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                    raise LimitsUnavailable(f"landlock_add_rule({d}) failed: {os.strerror(ctypes.get_errno())}")
            finally:
                os.close(fd)
        if libc.syscall(SYS_LL_RESTRICT, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise LimitsUnavailable(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)
    return abi


def _seccomp() -> None:
    """Refuse socket creation, ptrace, cross-process memory access and
    io_uring with EPERM; kill the process on any other syscall ABI."""
    import ctypes
    import platform
    arch = _SECCOMP_ARCH.get(platform.machine())
    if arch is None:
        raise LimitsUnavailable(f"no seccomp filter for {platform.machine()}")
    audit_arch, nrs = arch
    LD_ABS, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
    ALLOW, KILL, DENY = 0x7FFF0000, 0x80000000, 0x00050000 | 1    # DENY = errno EPERM
    prog = [(LD_ABS, 0, 0, 4), (JEQ, 1, 0, audit_arch), (RET, 0, 0, KILL), (LD_ABS, 0, 0, 0)]
    checks = [(JEQ, nr) for nr in sorted(nrs.values())]
    if audit_arch == 0xC000003E:
        checks.append((JGE, 0x40000000))        # x32 syscalls: another way in, refuse them all
    deny_at = len(prog) + len(checks) + 1       # after the checks and the ALLOW
    for op, k in checks:
        prog.append((op, deny_at - len(prog) - 1, 0, k))
    prog += [(RET, 0, 0, ALLOW), (RET, 0, 0, DENY)]

    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    filters = (SockFilter * len(prog))(*[SockFilter(*ins) for ins in prog])
    fprog = SockFprog(len(prog), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise LimitsUnavailable(f"seccomp filter could not be installed: {os.strerror(ctypes.get_errno())}")


def _kernel_lockdown(read_roots: list[str], simulate_failure: str = "") -> None:
    """Linux: Landlock + seccomp, then prove both hold. Elsewhere: nothing
    (Windows relies on the Job Object and the audit hook)."""
    if not sys.platform.startswith("linux"):
        if simulate_failure == "LANDLOCK":
            raise LimitsUnavailable("Landlock could not be applied (simulated)")
        return
    import ctypes
    import _socket
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_NO_NEW_PRIVS = 38
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise LimitsUnavailable("prctl(NO_NEW_PRIVS) failed")
    if simulate_failure == "LANDLOCK":
        raise LimitsUnavailable("Landlock could not be applied (simulated)")
    _landlock(sorted(set(read_roots) | set(_SYSTEM_LIB_DIRS)))
    _seccomp()
    # Prove it: each of these must now fail.
    probes = {
        "reading /proc": lambda: open("/proc/self/environ", "rb").close(),
        "writing a file": lambda: open(os.path.join(os.getcwd(), ".lockdown-probe"), "wb").close(),
        "opening a socket": lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM).close(),
    }
    for what, probe in probes.items():
        try:
            probe()
        except OSError:
            continue
        raise LimitsUnavailable(f"kernel lockdown is not enforced ({what} still works)")


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


def _refuse(job: dict, out, e: Exception) -> None:
    """No agent code has run. The parent turns this into a refusal."""
    out.write("\n" + job["marker"] + json.dumps(
        {"ok": False, "refused": True, "stdout": "",
         "error": f"Sandbox refused to run: {e}. The code was not executed."}) + "\n")
    out.flush()


def main() -> None:
    real_stdout = sys.stdout
    job = json.loads(sys.stdin.read())
    simulate = job.get("simulate_limit_failure", "")
    try:
        _apply_limits(job["cpu_seconds"], job["mem_mb"], simulate)
    except LimitsUnavailable as e:
        _refuse(job, real_stdout, e)
        return

    # --- load data while file access is still allowed -------------------
    import numpy as np  # noqa: F401  (pre-import so lazy loads are rare)
    import pandas as pd
    import statistics  # noqa: F401

    prices = pd.read_pickle(job["data_path"])
    universe = dict(job["universe"])
    read_roots = sorted({sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix})

    # --- lock down -------------------------------------------------------
    try:
        _kernel_lockdown(read_roots, simulate)
    except LimitsUnavailable as e:
        _refuse(job, real_stdout, e)
        return
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
