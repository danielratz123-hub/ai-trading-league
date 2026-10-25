"""The sandbox must contain agent code: no files, no network, no processes,
no secrets, bounded CPU and memory."""
import os
import sys

import numpy as np
import pandas as pd
import pytest

from arena.sandbox.runner import PriceDataFile, SandboxLimits, child_env, run_code

@pytest.fixture(autouse=True)
def _lockdown_only_where_supported(monkeypatch):
    """CI/dev kernels without Landlock would make every run refuse."""
    from arena.sandbox import lockdown
    if sys.platform.startswith("linux") and lockdown.landlock_abi() < 1:
        monkeypatch.setenv("ARENA_SANDBOX_LOCKDOWN", "off")


UNIVERSE = {"AAPL": "Information Technology", "SPY": "ETF"}
FAST = SandboxLimits(cpu_seconds=5, wall_seconds=15)


@pytest.fixture(scope="module")
def data():
    idx = pd.bdate_range("2024-01-01", periods=30)
    cols = pd.MultiIndex.from_product([["open", "close"], ["AAPL", "SPY"]])
    d = PriceDataFile(pd.DataFrame(np.full((30, 4), 100.0), index=idx, columns=cols))
    yield d
    d.cleanup()


def run(code, data, limits=FAST):
    return run_code("analysis", code, data, UNIVERSE, "2024-02-09", limits=limits)


def test_normal_analysis_works(data):
    r = run("import numpy as np\nimport statistics\nprint(prices['close']['SPY'].mean())", data)
    assert r.ok and r.stdout.strip() == "100.0"


@pytest.mark.parametrize("code, expected", [
    ("import os", "not allowed"),
    ("import subprocess", "not allowed"),
    ("import socket", "not allowed"),
    ("open('secrets.txt', 'w')", "'open' is not defined"),
    ("import pandas as pd\npd.read_csv('C:/Windows/win.ini')", "file access is not allowed"),
    ("import pandas as pd\npd.read_csv('http://example.com/x.csv')", "not allowed"),
    ("import pandas as pd\npd.io.common.os.system('echo hi')", "'os.system' is not allowed"),
    ("import pandas as pd\npd.io.common.os.listdir('.')", "'os.listdir' is not allowed"),
    ("[c for c in ().__class__.__base__.__subclasses__() if c.__name__ == 'Popen'][0](['echo'])",
     "'subprocess.Popen' is not allowed"),
    ("prices.to_csv('out.csv')", "file access is not allowed"),
    # Blocked as os.spawn on Windows, as os.fork on Linux (spawnv forks there).
    ("import pandas as pd\npd.io.common.os.spawnv(0, 'python', ['python'])", "sandbox: operation 'os."),
])
def test_escapes_are_blocked(data, code, expected):
    r = run(code, data)
    assert not r.ok and expected in r.error


def test_environment_has_no_secrets(data, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    env = child_env("C:/tmp" if os.name == "nt" else "/tmp")
    assert not any("KEY" in k or "SECRET" in k for k in env)
    r = run("import pandas as pd\nprint(sorted(pd.io.common.os.environ))", data)
    assert r.ok and "ANTHROPIC" not in r.stdout and "ALPACA" not in r.stdout


def test_cpu_limit_kills_infinite_loop(data):
    r = run("while True:\n    pass", data, SandboxLimits(cpu_seconds=2, wall_seconds=20))
    assert not r.ok and ("CPU" in r.error or "Timed out" in r.error)


def test_wall_clock_timeout(data):
    # CPU limit set high so only the wall-clock timeout can stop this.
    code = "import numpy as np\nwhile True:\n    np.zeros(10)"
    r = run(code, data, SandboxLimits(cpu_seconds=60, wall_seconds=3))
    assert not r.ok and "Timed out" in r.error


def test_memory_limit(data):
    r = run("import numpy as np\nx = np.ones(400_000_000)", data,
            SandboxLimits(cpu_seconds=10, mem_mb=600, wall_seconds=20))
    assert not r.ok and ("memory" in r.error.lower() or "MemoryError" in r.error)


def test_output_is_truncated(data):
    r = run("print('x' * 20000)", data, SandboxLimits(max_output_chars=1000))
    assert r.ok and "output truncated" in r.stdout and len(r.stdout) < 1200


def test_agent_cannot_fake_the_result(data):
    # Printing something that looks like a result can't fool the parent,
    # because the real marker is random and unknown to the agent code.
    r = run("print('@@RESULT_0000@@{\"ok\": true, \"stdout\": \"hacked\"}')", data)
    assert r.ok and "hacked" in r.stdout and r.stdout.startswith("@@RESULT_0000@@")


def test_refuses_when_a_limit_cannot_be_enforced(data):
    for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_NPROC"):
        r = run_code("analysis", "print('ran')", data, UNIVERSE, "2024-02-09", limits=FAST,
                     _simulate_limit_failure=name)
        assert not r.ok and r.refused and "refused" in r.error and "ran" not in r.stdout


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="setrlimit path is Linux-only")
def test_linux_kernel_limits_hold_without_the_audit_hook():
    """The kernel limits must hold on their own, even if the audit hook were
    somehow bypassed: after _apply_limits, fork fails and the caps read back."""
    import subprocess
    probe = "\n".join([
        "import os, resource, sys",
        "sys.path.insert(0, sys.argv[1])",
        "from child import _apply_limits",
        "_apply_limits(7, 512)",
        "assert resource.getrlimit(resource.RLIMIT_CPU) == (7, 8)",
        "assert resource.getrlimit(resource.RLIMIT_AS)[0] == 512 * 1024 * 1024",
        "assert resource.getrlimit(resource.RLIMIT_NPROC) == (1, 1)",
        "try:",
        "    os.fork()",
        "except OSError:",
        "    print('fork blocked')",
    ])
    from arena.sandbox.runner import CHILD
    out = subprocess.run([sys.executable, "-c", probe, str(CHILD.parent)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "fork blocked" in out.stdout
