"""Backups: consistent copies of a live WAL database, retention, and a full
restore that gives back the same league."""
import gzip
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from arena import backup, ledger
from arena.backup import BackupError, DirStorage, fetch_backup, run_backup, to_delete
from arena.db import connect
from arena.prices import ET


@pytest.fixture
def live(tmp_path):
    """A league database in WAL mode with an open writer, like the engine."""
    path = tmp_path / "data" / "arena.db"
    path.parent.mkdir()
    conn = connect(path)
    ledger.create_agent(conn, "claude", 1000, "2024-01-05")
    ledger.create_agent(conn, "gpt", 1000, "2024-01-05")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    yield path, conn
    conn.close()


def counts(path):
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("agents", "slots", "deposits", "events_log")}
    finally:
        c.close()


def test_backup_includes_changes_still_in_the_wal(live, tmp_path):
    path, conn = live
    conn.execute("PRAGMA wal_autocheckpoint=0")          # keep new rows in the -wal file only
    conn.execute("INSERT INTO events_log(ts, level, source, message) VALUES ('t', 'info', 'test', 'only in wal')")
    conn.commit()
    assert (path.parent / "arena.db-wal").stat().st_size > 0
    res = run_backup(path, storage=None)
    out = tmp_path / "check.db"
    backup.decompress(res.local, out)
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM events_log WHERE message='only in wal'").fetchone()[0] == 1
    c.close()


def test_retention_keeps_newest_30_daily_and_12_monthly():
    daily = [f"arena/daily/arena-2026-{m:02d}-{d:02d}.db.gz" for m in (8, 9) for d in range(1, 31)]
    gone = to_delete(daily, 30)
    assert len(gone) == 30 and all("-08-" in g for g in gone)
    monthly = [f"arena/monthly/arena-{y}-{m:02d}.db.gz" for y in (2025, 2026) for m in range(1, 13)]
    gone = to_delete(monthly, 12)
    assert gone == monthly[:12]
    assert to_delete(["x.txt", "arena/daily/arena-2026-01-01.db.gz"], 30) == []


def test_daily_monthly_upload_and_pruning(live, tmp_path):
    path, _ = live
    store = DirStorage(tmp_path / "bucket")
    start = datetime(2026, 1, 1, 17, 30, tzinfo=ET)
    for i in range(45):                                  # 45 days of backups
        run_backup(path, store, prefix="p/", keep_daily=30, keep_monthly=12, now=start + timedelta(days=i))
    daily = store.list("p/daily/")
    monthly = store.list("p/monthly/")
    assert len(daily) == 30 and daily[-1].endswith("2026-02-14.db.gz")
    assert monthly == ["p/monthly/arena-2026-01.db.gz", "p/monthly/arena-2026-02.db.gz"]
    assert len(list((path.parent / "backups").glob("arena-*.db.gz"))) == backup.LOCAL_KEEP
    with gzip.open(store.root / daily[-1]) as g:
        assert g.read(16) == b"SQLite format 3\x00"     # compressed SQLite file


def test_full_restore_on_a_copy_gives_back_the_same_league(live, tmp_path):
    path, conn = live
    store = DirStorage(tmp_path / "bucket")
    run_backup(path, store, prefix="p/")
    before = counts(path)
    # Restore through the real command line, as restore.sh does.
    target = tmp_path / "restored" / "arena.db"
    target.parent.mkdir()
    name = fetch_backup("latest", target, path, store, prefix="p/")
    assert name.startswith("p/daily/")
    assert counts(target) == before
    restored = connect(target)
    assert sorted(ledger.agent_ids(restored)) == ["claude", "gpt"]
    assert ledger.value_agent(restored, "claude", "2024-01-05").equity == pytest.approx(1000)
    restored.close()


def test_restore_refuses_a_damaged_backup(live, tmp_path):
    path, _ = live
    store = DirStorage(tmp_path / "bucket")
    run_backup(path, store, prefix="p/")
    key = store.list("p/daily/")[0]
    (store.root / key).write_bytes(gzip.compress(b"not a database" * 100))
    with pytest.raises(BackupError):
        fetch_backup(key, tmp_path / "out.db", path, store, prefix="p/")
    assert not (tmp_path / "out.db").exists()


def test_restore_command_never_writes_over_the_live_database(live):
    path, _ = live
    r = subprocess.run([sys.executable, "-m", "arena.main", "restore-backup", "latest",
                        "--to", str(path), "--db", str(path)], capture_output=True, text=True)
    assert r.returncode != 0 and "must not be the live database" in r.stderr


def test_backup_command_local_only(live):
    path, _ = live
    r = subprocess.run([sys.executable, "-m", "arena.main", "backup", "--db", str(path)],
                       capture_output=True, text=True, env={**__import__("os").environ, "BACKUP_S3_BUCKET": ""})
    assert r.returncode == 0, r.stderr
    assert "local copy only" in r.stdout
