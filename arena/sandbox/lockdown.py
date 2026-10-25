"""Linux kernel lockdown for the sandbox child: Landlock + seccomp-bpf.

Stdlib + ctypes only. This file must NEVER import the `arena` package (same
rule as child.py: arena.config loads .env, and with it the API keys).

Why: Docker blocks `unshare`, so the network namespace trick doesn't work on
the server, and the audit hook alone is a Python-level guard. These two are
enforced by the kernel and cannot be undone by the process:

  * Landlock: the process may only READ files under Python's own library
    folders. No /proc, no /dev, no temp dir, no writes. With Landlock ABI 4+
    (Linux 6.7) TCP bind/connect are refused too.
  * seccomp-bpf: socket, socketpair, connect, bind, ptrace, process_vm_*
    and io_uring_* return EPERM. Any other CPU architecture is killed.

apply_and_verify() applies both and then PROVES it by trying forbidden
things. If anything is missing or any probe succeeds it raises
LockdownError and the child must refuse to run the agent's code.
On Windows/macOS it does nothing (other layers apply there).
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
import sys
import sysconfig

LOCKDOWN_EXIT_CODE = 86     # the child exits with this when the lockdown fails

# --- syscall numbers (the three Landlock ones are the same on every arch) ----
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

PR_GET_SECCOMP = 21
PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2

# --- Landlock constants ------------------------------------------------------
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem rights by the ABI that introduced them (bit numbers).
_FS_BITS_BY_ABI = [
    (1, range(0, 13)),   # EXECUTE .. MAKE_SYM
    (2, [13]),           # REFER
    (3, [14]),           # TRUNCATE
    (5, [15]),           # IOCTL_DEV
]
FS_EXECUTE = 1 << 0
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1
NET_ABI = 4

# --- seccomp -----------------------------------------------------------------
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
X32_SYSCALL_BIT = 0x40000000

BPF_LD_W_ABS = 0x20         # BPF_LD | BPF_W | BPF_ABS
BPF_JEQ_K = 0x15            # BPF_JMP | BPF_JEQ | BPF_K
BPF_JGE_K = 0x35            # BPF_JMP | BPF_JGE | BPF_K
BPF_RET_K = 0x06            # BPF_RET | BPF_K
OFFSET_NR, OFFSET_ARCH = 0, 4   # fields of struct seccomp_data

_NET_AND_SPY = ("socket", "socketpair", "connect", "bind", "ptrace", "process_vm_readv",
                "process_vm_writev", "io_uring_setup", "io_uring_enter", "io_uring_register")
SYSCALLS = {
    "x86_64": {"arch": AUDIT_ARCH_X86_64, "nr": {
        "socket": 41, "connect": 42, "bind": 49, "socketpair": 53, "ptrace": 101,
        "process_vm_readv": 310, "process_vm_writev": 311,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427}},
    "aarch64": {"arch": AUDIT_ARCH_AARCH64, "nr": {
        "socket": 198, "socketpair": 199, "bind": 200, "connect": 203, "ptrace": 117,
        "process_vm_readv": 270, "process_vm_writev": 271,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427}},
}
assert all(set(v["nr"]) == set(_NET_AND_SPY) for v in SYSCALLS.values())


class LockdownError(RuntimeError):
    """The lockdown could not be applied or verified: refuse to run."""


# ---------------------------------------------------------------------------
# Pure builders (unit-tested without touching the kernel)
# ---------------------------------------------------------------------------

def fs_rights(abi: int) -> int:
    """Every filesystem access right known to this Landlock ABI."""
    mask = 0
    for since, bits in _FS_BITS_BY_ABI:
        if abi >= since:
            for b in bits:
                mask |= 1 << b
    return mask


def library_roots(extra: list[str] | None = None) -> list[str]:
    """Existing folders the agent may read: Python's library folders, the
    site-packages that hold pandas/numpy (pass their parent dirs in `extra`)
    and the lib dirs of the base installation. Nested folders are dropped."""
    cands = [sysconfig.get_path(n) for n in ("stdlib", "platstdlib", "purelib", "platlib")]
    for prefix in {sys.base_prefix, sys.base_exec_prefix}:
        cands += [os.path.join(prefix, "lib"), os.path.join(prefix, "lib64")]
    cands += list(extra or [])
    real = {os.path.realpath(c) for c in cands if c and os.path.isdir(c)}
    return sorted(p for p in real
                  if not any(p != q and p.startswith(q.rstrip(os.sep) + os.sep) for q in real))


def path_rules(paths: list[str], handled: int) -> list[tuple[str, int]]:
    """(path, allowed_access) pairs: read file + read dir only, and never more
    than the ruleset handles."""
    allowed = (FS_READ_FILE | FS_READ_DIR) & handled
    return [(p, allowed) for p in paths]


def build_bpf(machine: str) -> bytes:
    """The seccomp program as raw struct sock_filter bytes.

    1. wrong architecture -> kill (stops the 32-bit-syscall-table bypass)
    2. x86_64 only: x32 syscall numbers (bit 30 set) -> kill
    3. listed syscalls -> EPERM
    4. everything else -> allow"""
    spec = SYSCALLS.get(machine)
    if spec is None:
        raise LockdownError(f"seccomp filter not available for CPU architecture {machine!r}")
    nrs = sorted(spec["nr"].values())
    prog: list[tuple[int, int, int, int]] = [
        (BPF_LD_W_ABS, 0, 0, OFFSET_ARCH),
        (BPF_JEQ_K, 1, 0, spec["arch"]),                    # right arch: skip the kill
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, OFFSET_NR),
    ]
    if machine == "x86_64":
        prog += [(BPF_JGE_K, 0, 1, X32_SYSCALL_BIT),
                 (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS)]
    n = len(nrs)
    for i, nr in enumerate(nrs):
        # if equal, jump over the remaining compares and the ALLOW to the EPERM
        prog.append((BPF_JEQ_K, n - i, 0, nr))
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    prog.append((BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM))
    return b"".join(struct.pack("=HBBI", *ins) for ins in prog)


# ---------------------------------------------------------------------------
# Kernel calls
# ---------------------------------------------------------------------------

def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _syscall(libc, nr, *args) -> int:
    libc.syscall.restype = ctypes.c_long
    r = libc.syscall(ctypes.c_long(nr), *args)
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r


def landlock_abi() -> int:
    """Highest Landlock ABI version the kernel offers, or 0 if none."""
    try:
        return _syscall(_libc(), SYS_LANDLOCK_CREATE_RULESET, None, 0,
                        LANDLOCK_CREATE_RULESET_VERSION)
    except OSError:
        return 0


def _apply_landlock(abi: int, roots: list[str]) -> bool:
    """Returns True if TCP rules are in force too (ABI >= 4)."""
    libc = _libc()
    handled = fs_rights(abi)
    net = abi >= NET_ABI
    size = 8 if abi < NET_ABI else 16
    attr = struct.pack("=QQ", handled, NET_BIND_TCP | NET_CONNECT_TCP if net else 0)[:size]
    buf = ctypes.create_string_buffer(attr, len(attr))
    fd = _syscall(libc, SYS_LANDLOCK_CREATE_RULESET, buf, ctypes.c_size_t(size), 0)
    try:
        for path, allowed in path_rules(roots, handled):
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = ctypes.create_string_buffer(struct.pack("=Qi", allowed, pfd), 12)
                _syscall(libc, SYS_LANDLOCK_ADD_RULE, ctypes.c_int(fd),
                         ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), rule, 0)
            finally:
                os.close(pfd)
        _syscall(libc, SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(fd), 0)
    finally:
        os.close(fd)
    return net


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _apply_seccomp(machine: str) -> None:
    prog = build_bpf(machine)
    buf = ctypes.create_string_buffer(prog, len(prog))
    fprog = _SockFprog(len(prog) // 8, ctypes.cast(buf, ctypes.c_void_p))
    libc = _libc()
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


# ---------------------------------------------------------------------------
# Probes: prove the lockdown holds
# ---------------------------------------------------------------------------

def _must_fail_open(path: str, flags: int, what: str) -> None:
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return
    os.close(fd)
    if flags & os.O_CREAT:
        try:
            os.unlink(path)
        except OSError:
            pass
    raise LockdownError(f"self-check failed: {what} was allowed")


def _probe_tcp_connect(socket_mod) -> None:
    """Landlock only: seccomp isn't on yet, so a socket can be created and the
    connect itself must be refused with a permission error."""
    s = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    try:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", 9))
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                return
            raise LockdownError(f"self-check failed: TCP connect was not blocked ({e})") from None
        raise LockdownError("self-check failed: TCP connect to 127.0.0.1 succeeded")
    finally:
        s.close()


def _probe_after_lockdown(socket_mod, cwd: str) -> None:
    for path in ("/proc/1/environ", "/proc/self/environ"):
        _must_fail_open(path, os.O_RDONLY, f"reading {path}")
    _must_fail_open(os.path.join(cwd, f".lockdown_probe_{os.getpid()}"),
                    os.O_WRONLY | os.O_CREAT, "writing a file")
    try:
        s = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES):
            raise LockdownError(f"self-check failed: socket() failed with {e}, not EPERM") from None
    else:
        s.close()
        raise LockdownError("self-check failed: a socket could be created")
    if _libc().prctl(PR_GET_SECCOMP, 0, 0, 0, 0) != 2:
        raise LockdownError("self-check failed: seccomp filter is not active")


def apply_and_verify(extra_read_dirs: list[str] | None = None) -> str:
    """Lock this process down and prove it. Raises LockdownError otherwise.
    Call after all imports/data loading and before running agent code.
    Returns a short description of what is in force."""
    if not sys.platform.startswith("linux"):
        return "not linux: kernel lockdown skipped"
    import socket           # import now: the probes need it after files are closed to us
    machine = platform.machine().lower()
    if machine == "arm64":
        machine = "aarch64"
    if machine not in SYSCALLS:
        raise LockdownError(f"unsupported CPU architecture {machine!r}")
    abi = landlock_abi()
    if abi < 1:
        raise LockdownError("Landlock is not available (needs Linux 5.13+ with Landlock enabled)")
    roots = library_roots(extra_read_dirs)
    if not roots:
        raise LockdownError("no Python library folders found to allow")
    cwd = os.getcwd()
    try:
        libc = _libc()
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        net = _apply_landlock(abi, roots)
        if net:
            _probe_tcp_connect(socket)
        _apply_seccomp(machine)
    except OSError as e:
        raise LockdownError(f"could not apply lockdown ({e})") from None
    _probe_after_lockdown(socket, cwd)
    return f"landlock abi {abi}{' (tcp blocked)' if net else ''} + seccomp"
