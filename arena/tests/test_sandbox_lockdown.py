"""Landlock + seccomp lockdown of the sandbox child. No network, no root."""
import errno
import logging
import os
import struct
import subprocess
import sys
import textwrap
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from arena import alerts
from arena.sandbox import lockdown, runner
from arena.sandbox.runner import PriceDataFile, SandboxLimits, run_code

LINUX = sys.platform.startswith("linux")
needs_kernel = pytest.mark.skipif(
    not LINUX or lockdown.landlock_abi() < 1, reason="kernel has no Landlock")


def _decode(blob):
    return [struct.unpack("=HBBI", blob[i:i + 8]) for i in range(0, len(blob), 8)]


# ----- pure builders ---------------------------------------------------------

def test_fs_rights_grow_with_abi():
    assert lockdown.fs_rights(1) == (1 << 13) - 1
    assert lockdown.fs_rights(3) == (1 << 15) - 1
    assert lockdown.fs_rights(5) == (1 << 16) - 1


def test_path_rules_are_read_only():
    rules = lockdown.path_rules(["/a", "/b"], lockdown.fs_rights(4))
    assert [p for p, _ in rules] == ["/a", "/b"]
    for _, allowed in rules:
        assert allowed == lockdown.FS_READ_FILE | lockdown.FS_READ_DIR
        assert not allowed & (lockdown.FS_EXECUTE | 1 << 1)   # no exec, no write


def test_library_roots_drop_missing_and_nested(tmp_path):
    (tmp_path / "site" / "pkg").mkdir(parents=True)
    roots = lockdown.library_roots([str(tmp_path / "site"), str(tmp_path / "site" / "pkg"),
                                    str(tmp_path / "nope")])
    assert os.path.realpath(tmp_path / "site") in roots
    assert os.path.realpath(tmp_path / "site" / "pkg") not in roots
    assert not any("nope" in r for r in roots)
    assert not any(r.startswith(("/proc", "/dev")) for r in roots)


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_bpf_program(machine):
    spec = lockdown.SYSCALLS[machine]
    prog = _decode(lockdown.build_bpf(machine))
    assert prog[0] == (lockdown.BPF_LD_W_ABS, 0, 0, 4)                   # load arch
    assert prog[1] == (lockdown.BPF_JEQ_K, 1, 0, spec["arch"])           # arch check
    assert prog[2] == (lockdown.BPF_RET_K, 0, 0, lockdown.SECCOMP_RET_KILL_PROCESS)
    compares = [i for i in prog if i[0] == lockdown.BPF_JEQ_K][1:]
    assert {i[3] for i in compares} == set(spec["nr"].values())
    assert len(compares) == 10
    assert prog[-2] == (lockdown.BPF_RET_K, 0, 0, lockdown.SECCOMP_RET_ALLOW)
    assert prog[-1] == (lockdown.BPF_RET_K, 0, 0, lockdown.SECCOMP_RET_ERRNO | errno.EPERM)
    # every matching compare must land exactly on the EPERM return
    for idx, ins in enumerate(prog):
        if ins in compares:
            assert idx + 1 + ins[1] == len(prog) - 1
    assert (machine == "x86_64") == any(i[3] == lockdown.X32_SYSCALL_BIT for i in prog)


def test_bpf_rejects_unknown_arch():
    with pytest.raises(lockdown.LockdownError):
        lockdown.build_bpf("riscv64")


# ----- real kernel -----------------------------------------------------------

PROBE = textwrap.dedent("""
    import importlib.util, os, sys, errno
    import numpy, pandas
    spec = importlib.util.spec_from_file_location("lk", sys.argv[1])
    lk = importlib.util.module_from_spec(spec); sys.modules["lk"] = lk; spec.loader.exec_module(lk)
    import socket
    print(lk.apply_and_verify([os.path.dirname(os.path.dirname(m.__file__)) for m in (numpy, pandas)]))
    def fails(fn):
        try: fn()
        except OSError as e: return e.errno
        return None
    print(fails(lambda: os.open("/proc/self/environ", os.O_RDONLY)))
    print(fails(lambda: os.open("probe.txt", os.O_WRONLY | os.O_CREAT)))
    print(fails(lambda: os.open("/tmp/probe.txt", os.O_WRONLY | os.O_CREAT)))
    print(fails(lambda: socket.socket()))
    print(fails(lambda: os.listdir("/dev")))
    print(numpy.arange(4).sum(), pandas.Series([1, 2]).sum())
""")


@needs_kernel
def test_real_child_is_locked_down(tmp_path):
    p = subprocess.run([sys.executable, "-I", "-c", PROBE, lockdown.__file__],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={"PATH": os.path.dirname(sys.executable)})
    assert p.returncode == 0, p.stderr
    desc, environ, wr, tmpwr, sock, dev, calc = p.stdout.strip().splitlines()
    assert "seccomp" in desc
    assert int(environ) in (errno.EACCES, errno.EPERM)
    assert int(wr) in (errno.EACCES, errno.EPERM)
    assert int(tmpwr) in (errno.EACCES, errno.EPERM)
    assert int(sock) in (errno.EACCES, errno.EPERM)
    assert int(dev) in (errno.EACCES, errno.EPERM)
    assert calc == "6 3"
    assert not (tmp_path / "probe.txt").exists()


@pytest.fixture
def data():
    idx = pd.bdate_range("2024-01-01", periods=10)
    cols = pd.MultiIndex.from_product([["close"], ["AAPL"]])
    d = PriceDataFile(pd.DataFrame(np.full((10, 1), 100.0), index=idx, columns=cols))
    yield d
    d.cleanup()


@needs_kernel
def test_agent_code_cannot_reach_files_or_sockets(data, monkeypatch):
    monkeypatch.delenv("ARENA_SANDBOX_LOCKDOWN", raising=False)
    code = ("import pandas as pd\n"
            "for p in ('/proc/self/environ', '/proc/1/environ'):\n"
            "    try:\n        pd.read_csv(p); print('READ')\n"
            "    except Exception: print('blocked')\n"
            "try:\n    pd.DataFrame({'a': [1]}).to_csv('out.csv'); print('WROTE')\n"
            "except Exception: print('blocked')\n"
            "try:\n    import socket; print('SOCKET')\n"
            "except Exception: print('blocked')\n"
            "print(prices['close']['AAPL'].mean())\n")
    r = run_code("analysis", code, data, {"AAPL": "IT"}, "2024-01-10",
                 limits=SandboxLimits(cpu_seconds=5, wall_seconds=30))
    assert r.ok and not r.refused, r.error
    assert r.stdout.split() == ["blocked"] * 4 + ["100.0"]


# ----- the runner refuses when the lockdown fails ----------------------------

def test_lockdown_switch(monkeypatch):
    monkeypatch.setenv("ARENA_SANDBOX_LOCKDOWN", "off")
    assert not runner.lockdown_required()
    monkeypatch.setenv("ARENA_SANDBOX_LOCKDOWN", "")
    assert runner.lockdown_required() == LINUX


class _FakeProc:
    returncode = lockdown.LOCKDOWN_EXIT_CODE

    def __init__(self, *a, **k):
        pass

    def communicate(self, *a, **k):
        return b"", b"sandbox lockdown failed: Landlock is not available\n"


def test_runner_refuses_logs_and_alerts(data, monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(runner.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(alerts, "send", lambda *a, **k: sent.append((a, k)) or True)
    monkeypatch.setattr(runner, "lockdown_required", lambda: True)
    with caplog.at_level(logging.ERROR, logger="arena.sandbox"):
        r = run_code("analysis", "print(1)", data, {"AAPL": "IT"}, "2024-01-10")
    assert r.refused and not r.ok and r.stdout == ""
    assert "Landlock is not available" in r.error and "not executed" in r.error
    assert "Landlock is not available" in caplog.text
    (args, kw), = sent
    assert kw["level"] == "error" and kw["every"] == timedelta(hours=1)
    assert "kernel lockdown failed" in args[0] and "Landlock is not available" not in args[0]


def test_runner_handles_a_real_process_exiting_86(data, monkeypatch):
    """Real subprocess path (no fake Popen), with a stand-in child."""
    sent = []
    monkeypatch.setattr(alerts, "send", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(runner, "lockdown_required", lambda: True)
    # A stand-in child that fails the lockdown the same way child.py does.
    fake = data.workdir + "/fake_child.py"
    with open(fake, "w") as f:
        f.write("import sys\nsys.stderr.write('sandbox lockdown failed: test\\n')\nsys.exit(86)\n")
    monkeypatch.setattr(runner, "CHILD", type(runner.CHILD)(fake))
    r = run_code("analysis", "print(1)", data, {"AAPL": "IT"}, "2024-01-10")
    assert r.refused and "test" in r.error and len(sent) == 1
