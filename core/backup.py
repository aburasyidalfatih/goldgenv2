"""
Nightly SQLite backups: everything the app knows (API keys, Fanspage tokens, post
history, learned topic weights) lives in one database file.

SQLite's online backup API copies a consistent snapshot while the app keeps
running (WAL mode included). Each copy is integrity-checked, gzipped and kept for
BACKUP_KEEP_DAYS days.
"""
import gzip
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from config import BACKUP_DIR, DB_PATH

logger = logging.getLogger(__name__)

BACKUP_KEEP_DAYS = 14
BACKUP_PREFIX = "autoposter-"
BACKUP_SUFFIX = ".db.gz"
BACKUP_NAME = re.compile(r"^autoposter-\d{8}-\d{6}\.db\.gz$")   # also guards downloads against path tricks


def _backup_time(name: str) -> datetime:
    stamp = name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
    return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def list_backups() -> list:
    """Newest first: [{name, size, created_at}]."""
    items = []
    for path in BACKUP_DIR.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        if BACKUP_NAME.match(path.name):
            items.append({
                "name": path.name,
                "size": path.stat().st_size,
                "created_at": _backup_time(path.name).isoformat(),
            })
    return sorted(items, key=lambda b: b["name"], reverse=True)


def backup_path(name: str):
    """Path of an existing backup, or None for anything that is not one."""
    if not BACKUP_NAME.match(name or ""):
        return None
    path = BACKUP_DIR / name
    return path if path.is_file() else None


def create_backup() -> dict:
    """Writes a new compressed snapshot, then prunes old ones."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    name = f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}{BACKUP_SUFFIX}"
    snapshot = BACKUP_DIR / f".{name}.tmp.db"
    partial = BACKUP_DIR / f".{name}.part"
    try:
        source = sqlite3.connect(str(DB_PATH))
        target = sqlite3.connect(str(snapshot))
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
            source.close()
        if check != "ok":
            raise RuntimeError(f"pemeriksaan integritas gagal: {check}")

        with open(snapshot, "rb") as raw, gzip.open(partial, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)
        partial.replace(BACKUP_DIR / name)   # only complete files ever carry the real name
    finally:
        for leftover in (snapshot, partial):
            leftover.unlink(missing_ok=True)

    removed = prune_backups(now)
    size = (BACKUP_DIR / name).stat().st_size
    logger.info(f"[Backup] {name} written ({size} bytes); {removed} old backup(s) removed.")
    return {"name": name, "size": size, "created_at": now.isoformat(), "removed": removed}


def prune_backups(now: datetime | None = None, keep_days: int = BACKUP_KEEP_DAYS) -> int:
    """Deletes backups older than `keep_days`, always keeping the newest one."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    removed = 0
    for item in list_backups()[1:]:
        if _backup_time(item["name"]) < cutoff:
            (BACKUP_DIR / item["name"]).unlink(missing_ok=True)
            removed += 1
    return removed


def latest_backup_age_hours(now: datetime | None = None) -> float | None:
    backups = list_backups()
    if not backups:
        return None
    return ((now or datetime.now(timezone.utc)) - _backup_time(backups[0]["name"])).total_seconds() / 3600
