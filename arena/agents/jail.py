"""Landlock jail for an agent CLI on Linux: it may read anything, but write
only inside the folders it is given.

    python -I arena/agents/jail.py --write DIR [--write DIR ...] -- COMMAND ARGS...

Why: inside Docker, Codex's own read-only sandbox (bubblewrap) can't start,
and without it Codex's apply_patch tool can write files - tested on the
server, it could write into /app/data next to the database. Landlock is a
Linux security module that an unprivileged process can apply to itself (no
capabilities, works in a default container). The rules are inherited by
every process the command starts and can never be loosened.

The league's tool server is NOT started inside the jail: for a jailed agent
it runs outside, over a local HTTP address with a secret token (see
mcp_server.py --http), so the agent can't touch the database or the turn's
result file even though the tool server can.

Fails closed: if Landlock isn't available, the command is not started.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys

# Syscall numbers (the same on x86_64 and aarch64).
SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

# Filesystem rights that change something (reading and executing stay allowed).
WRITE_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 4, 1 << 5
MAKE_RIGHTS = sum(1 << b for b in range(6, 13))     # char, dir, reg, sock, fifo, block, sym
REFER, TRUNCATE = 1 << 13, 1 << 14                  # ABI 2, ABI 3


class JailError(RuntimeError):
    pass


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    """Landlock ABI version, or 0 if this kernel/container doesn't offer it."""
    if not sys.platform.startswith("linux") or platform.machine() not in ("x86_64", "aarch64"):
        return 0
    v = _libc().syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    return max(int(v), 0)


def write_rights(abi: int) -> int:
    rights = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_RIGHTS
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    return rights


def restrict_writes_to(paths: list[str]) -> None:
    """Apply the jail to THIS process (and everything it starts later)."""
    abi = abi_version()
    if abi < 1:
        raise JailError("Landlock is not available on this system; refusing to start the agent "
                        "without a filesystem jail.")
    libc = _libc()
    rights = write_rights(abi)
    attr = _RulesetAttr(rights)
    ruleset = libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                           ctypes.c_uint32(0))
    if ruleset < 0:
        raise JailError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for p in paths:
            fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneath(rights, fd)
                if libc.syscall(SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(RULE_PATH_BENEATH),
                                ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                    raise JailError(f"landlock_add_rule({p}) failed: {os.strerror(ctypes.get_errno())}")
            finally:
                os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise JailError(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
        if libc.syscall(SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise JailError(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset)


def prefix(writable: list[str | os.PathLike]) -> list[str]:
    """argv prefix that runs a command inside the jail."""
    # By file path, isolated (-I): it works from any folder and imports only
    # the standard library, never anything from the agent's scratch folder.
    out = [sys.executable, "-I", os.path.abspath(__file__)]
    for p in writable:
        out += ["--write", str(p)]
    return out + ["--"]


def main(argv: list[str]) -> None:
    writable, i = [], 0
    while i < len(argv) and argv[i] == "--write":
        writable.append(argv[i + 1])
        i += 2
    if i >= len(argv) or argv[i] != "--" or i + 1 >= len(argv):
        sys.exit("usage: python -I jail.py --write DIR ... -- COMMAND ...")
    cmd = argv[i + 1:]
    try:
        restrict_writes_to(writable)
    except (JailError, OSError) as e:
        print(f"jail: {e}", file=sys.stderr)
        sys.exit(126)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
