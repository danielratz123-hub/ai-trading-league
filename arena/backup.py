"""Daily database backups to S3-compatible storage (Backblaze B2, Wasabi, AWS...).

Why the online backup API and not a file copy: the database runs in WAL
mode, so recent changes can sit in arena.db-wal, and a plain copy of
arena.db taken while the app writes can be torn or incomplete. SQLite's
backup API copies a consistent snapshot through a normal connection, while
the app keeps running.

One backup:
  1. sqlite3 backup API -> a temporary file
  2. PRAGMA integrity_check on that copy (a broken backup is worse than none)
  3. gzip -> data/backups/arena-YYYY-MM-DD.db.gz (the last 7 are kept locally)
  4. upload as  <prefix>daily/arena-YYYY-MM-DD.db.gz
     and, if this month has none yet, also as  <prefix>monthly/arena-YYYY-MM.db.gz
  5. delete remote copies beyond the newest 30 daily and 12 monthly

Without BACKUP_S3_BUCKET only the local copies are made.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .prices import ET

LOCAL_KEEP = 7
_NAME = re.compile(r"(?:arena|config)-(\d{4}-\d{2}(?:-\d{2})?)\.(?:db\.gz|json)$")
_DB = re.compile(r"arena-(\d{4}-\d{2}(?:-\d{2})?)\.db\.gz$")


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Making and checking a backup file
# --------------------------------------------------------------------------

def snapshot(db_path: str | Path, dest: str | Path) -> None:
    """Consistent copy of a live WAL database via the online backup API."""
    src = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst, pages=1024)        # in steps, so the app is never blocked for long
    finally:
        src.close()
        dst.close()
    check_database(dest)


def check_database(path: str | Path) -> None:
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"integrity check failed: {result}")
        conn.execute("SELECT COUNT(*) FROM agents").fetchone()     # it is a league database
    except sqlite3.DatabaseError as e:
        raise BackupError(f"not a usable league database: {e}") from None
    finally:
        conn.close()


def compress(src: str | Path, dest: str | Path) -> None:
    with open(src, "rb") as f, gzip.open(dest, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1024 * 1024)


def decompress(src: str | Path, dest: str | Path) -> None:
    with gzip.open(src, "rb") as g, open(dest, "wb") as f:
        shutil.copyfileobj(g, f, 1024 * 1024)


# --------------------------------------------------------------------------
# Retention (pure: easy to test)
# --------------------------------------------------------------------------

def to_delete(names: list[str], keep: int) -> list[str]:
    """The names beyond the newest `keep` of each kind (database backups and
    config files are counted separately; names sort by date)."""
    out = []
    for kind in ("arena-", "config-"):
        dated = sorted((n for n in names if _NAME.search(n) and n.rsplit("/", 1)[-1].startswith(kind)),
                       key=lambda n: _NAME.search(n).group(1))
        out += dated[:-keep] if keep > 0 and len(dated) > keep else ([] if keep > 0 else dated)
    return out


# Settings worth restoring with the database. Secrets are never included:
# anything whose name looks like one is left out by name AND by pattern.
CONFIG_KEYS = ("AGENT_BACKEND", "CLAUDE_MODEL", "GPT_MODEL", "REASONING_EFFORT", "ALPACA_DATA_FEED",
               "MIRROR_MODE", "WEB_SEARCH_SOFT_LIMIT", "REPORTS", "REPORT_TIMEOUT_SEC", "REPORT_MAX_TOOL_CALLS",
               "DAILY_AI_COST_ALERT_USD", "CODEX_SANDBOX", "DOMAIN", "APP_UID", "BACKUP_KEEP_DAILY",
               "BACKUP_KEEP_MONTHLY", "BACKUP_RCLONE_REMOTE", "BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET",
               "BACKUP_S3_PREFIX", "GPT_PRICE_IN", "GPT_PRICE_CACHED_IN", "GPT_PRICE_OUT", "MAX_API_SPEND_USD")
_SECRETISH = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|HASH|CREDENTIAL)", re.I)


def config_snapshot() -> dict:
    """The league's non-secret settings and versions, for the backup."""
    import subprocess
    vals = {k: os.environ[k] for k in CONFIG_KEYS if k in os.environ and not _SECRETISH.search(k)}
    versions = {}
    for name, cmd in (("claude", ["claude", "--version"]), ("codex", ["codex", "--version"])):
        try:
            versions[name] = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            versions[name] = None
    return {"settings": vals, "cli_versions": versions,
            "note": "Secrets (API keys, tokens, password hash) are deliberately not included."}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

class Storage:
    """What the backup code needs from a bucket."""

    def put(self, local: Path, key: str) -> None: ...
    def get(self, key: str, local: Path) -> None: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class S3Storage(Storage):
    def __init__(self, bucket: str, endpoint: str | None, region: str | None,
                 key_id: str, secret: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint or None, region_name=region or None,
                               aws_access_key_id=key_id, aws_secret_access_key=secret,
                               config=Config(retries={"max_attempts": 5, "mode": "standard"},
                                             connect_timeout=20, read_timeout=120))

    def put(self, local, key):
        self.s3.upload_file(str(local), self.bucket, key)

    def get(self, key, local):
        self.s3.download_file(self.bucket, key, str(local))

    def list(self, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return out
            token = r.get("NextContinuationToken")

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)


class RcloneStorage(Storage):
    """Any rclone remote, e.g. Google Drive ("gdrive:ai-trading-league").
    The rclone config (with its Google sign-in) lives in the rclone-config
    volume; see DEPLOY.md "Backups"."""

    def __init__(self, remote: str):
        self.remote = remote.rstrip("/")

    def _run(self, *args: str) -> str:
        import subprocess
        p = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise BackupError(f"rclone {args[0]} failed: {p.stderr.strip()[-400:]}")
        return p.stdout

    def put(self, local, key):
        self._run("copyto", str(local), f"{self.remote}/{key}")

    def get(self, key, local):
        self._run("copyto", f"{self.remote}/{key}", str(local))

    def list(self, prefix):
        folder = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        try:
            names = self._run("lsf", "--files-only", f"{self.remote}/{folder}").split()
        except BackupError as e:
            if "directory not found" in str(e).lower():
                return []
            raise
        return sorted(f"{folder}/{n}" if folder else n for n in names if f"{folder}/{n}".startswith(prefix))

    def delete(self, key):
        self._run("deletefile", f"{self.remote}/{key}")


class DirStorage(Storage):
    """A folder that behaves like a bucket (tests, or a mounted disk)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, local, key):
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)

    def get(self, key, local):
        if not (self.root / key).exists():
            raise BackupError(f"no such backup: {key}")
        shutil.copyfile(self.root / key, local)

    def list(self, prefix):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix))

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)


def settings() -> dict:
    return {
        "rclone": os.getenv("BACKUP_RCLONE_REMOTE", "").strip(),
        "bucket": os.getenv("BACKUP_S3_BUCKET", "").strip(),
        "endpoint": os.getenv("BACKUP_S3_ENDPOINT", "").strip(),
        "region": os.getenv("BACKUP_S3_REGION", "").strip(),
        "key_id": os.getenv("BACKUP_S3_ACCESS_KEY_ID", "").strip(),
        "secret": os.getenv("BACKUP_S3_SECRET_ACCESS_KEY", "").strip(),
        "prefix": os.getenv("BACKUP_S3_PREFIX", "arena/").strip(),
        "keep_daily": int(os.getenv("BACKUP_KEEP_DAILY", "30")),
        "keep_monthly": int(os.getenv("BACKUP_KEEP_MONTHLY", "12")),
    }


def remote_storage() -> Storage | None:
    s = settings()
    if s["rclone"]:
        return RcloneStorage(s["rclone"])
    if not s["bucket"]:
        return None
    if not (s["key_id"] and s["secret"]):
        raise BackupError("BACKUP_S3_BUCKET is set but the access key id / secret are missing.")
    return S3Storage(s["bucket"], s["endpoint"], s["region"], s["key_id"], s["secret"])


# --------------------------------------------------------------------------
# The backup job
# --------------------------------------------------------------------------

@dataclass
class BackupResult:
    local: Path
    size: int
    uploaded: list[str]
    deleted: list[str]


def run_backup(db_path: str | Path, storage: Storage | None = None, prefix: str | None = None,
               keep_daily: int | None = None, keep_monthly: int | None = None,
               now: datetime | None = None) -> BackupResult:
    s = settings()
    prefix = s["prefix"] if prefix is None else prefix
    keep_daily = s["keep_daily"] if keep_daily is None else keep_daily
    keep_monthly = s["keep_monthly"] if keep_monthly is None else keep_monthly
    now = now or datetime.now(ET)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    local_dir = Path(db_path).resolve().parent / "backups"
    local_dir.mkdir(parents=True, exist_ok=True)
    local = local_dir / f"arena-{day}.db.gz"
    with tempfile.TemporaryDirectory(prefix="arena_backup_", dir=local_dir) as tmp:
        raw = Path(tmp) / "arena.db"
        snapshot(db_path, raw)
        compress(raw, Path(tmp) / "arena.db.gz")
        os.replace(Path(tmp) / "arena.db.gz", local)
    for old in to_delete([p.name for p in local_dir.glob("arena-*.db.gz")], LOCAL_KEEP):
        (local_dir / old).unlink(missing_ok=True)

    import json
    config = local_dir / f"config-{day}.json"
    config.write_text(json.dumps(config_snapshot(), indent=2), encoding="utf-8")
    for old in to_delete([p.name for p in local_dir.glob("config-*.json")], LOCAL_KEEP):
        (local_dir / old).unlink(missing_ok=True)

    uploaded: list[str] = []
    deleted: list[str] = []
    if storage is not None:
        daily_key = f"{prefix}daily/arena-{day}.db.gz"
        storage.put(local, daily_key)
        uploaded.append(daily_key)
        storage.put(config, f"{prefix}daily/config-{day}.json")
        uploaded.append(f"{prefix}daily/config-{day}.json")
        monthly = storage.list(f"{prefix}monthly/") if keep_monthly > 0 else []
        monthly_key = f"{prefix}monthly/arena-{month}.db.gz"
        if keep_monthly > 0 and monthly_key not in monthly:
            storage.put(local, monthly_key)          # first backup of the month
            uploaded.append(monthly_key)
            monthly.append(monthly_key)
        for kind, names, keep in (("daily", storage.list(f"{prefix}daily/"), keep_daily),
                                  ("monthly", monthly, keep_monthly if keep_monthly > 0 else 10**9)):
            for key in to_delete(names, keep):
                storage.delete(key)
                deleted.append(key)
    return BackupResult(local, local.stat().st_size, uploaded, deleted)


def list_backups(db_path: str | Path, storage: Storage | None, prefix: str | None = None) -> list[str]:
    prefix = settings()["prefix"] if prefix is None else prefix
    names = [f"local/{p.name}" for p in sorted((Path(db_path).resolve().parent / "backups").glob("arena-*.db.gz"))]
    if storage is not None:
        names += storage.list(f"{prefix}daily/") + storage.list(f"{prefix}monthly/")
    return names


def fetch_backup(name: str, dest: str | Path, db_path: str | Path, storage: Storage | None,
                 prefix: str | None = None) -> str:
    """Download (or take from local/), decompress and check a backup into
    `dest`. name: 'latest', a key from list_backups, or a file name."""
    prefix = settings()["prefix"] if prefix is None else prefix
    local_dir = Path(db_path).resolve().parent / "backups"
    if name == "latest":
        daily = [n for n in (storage.list(f"{prefix}daily/") if storage is not None else []) if _DB.search(n)]
        if daily:
            name = sorted(daily, key=lambda n: _DB.search(n).group(1))[-1]
        else:
            loc = sorted(p.name for p in local_dir.glob("arena-*.db.gz"))
            if not loc:
                raise BackupError("No backups found.")
            name = f"local/{loc[-1]}"
    dest = Path(dest)
    with tempfile.TemporaryDirectory(prefix="arena_restore_", dir=dest.parent) as tmp:
        gz = Path(tmp) / "backup.db.gz"
        if name.startswith("local/"):
            shutil.copyfile(local_dir / name.removeprefix("local/"), gz)
        elif storage is None:
            raise BackupError("Remote backups aren't configured (BACKUP_S3_*).")
        else:
            storage.get(name, gz)
        raw = Path(tmp) / "restored.db"
        decompress(gz, raw)
        check_database(raw)
        os.replace(raw, dest)
    return name
