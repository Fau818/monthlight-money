"""Daily copies of the database in the backups folder."""

import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

KEEP = 30  # daily copies to keep
COPIES = "money-????-??-??.db"


def make_daily_backup(db_path: Path, backup_dir: Path, today: date, keep: int = KEEP) -> Path | None:
    """Saves a complete copy of the database as backup_dir/money-YYYY-MM-DD.db unless today's copy exists, then
    deletes all but the newest `keep` copies. Returns the new copy's path, or None if there was nothing to do."""
    target = backup_dir / f"money-{today.isoformat()}.db"
    if target.exists() or not db_path.exists():
        return None
    # Build the copy beside the live database, so a backups folder that syncs (to iCloud, say) only ever sees a finished file.
    scratch = db_path.with_name("backup-in-progress.db")
    for leftover in db_path.parent.glob(scratch.name + "*"):
        leftover.unlink()
    source, copy = sqlite3.connect(db_path, timeout=30), sqlite3.connect(scratch)
    try:
        source.backup(copy)  # a consistent snapshot, even while the app is writing
        check = copy.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        copy.close()
        source.close()
    if check != "ok":
        raise RuntimeError(f"The backup copy failed its integrity check: {check}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".nosync")  # NOTE: sync services such as iCloud skip .nosync names, so they never see a half-written copy
    shutil.copyfile(scratch, partial)
    os.replace(partial, target)
    scratch.unlink()
    for old in sorted(backup_dir.glob(COPIES))[:-keep]:
        old.unlink()
    return target


def backup_summary(backup_dir: Path) -> dict:
    """Returns the newest copy's date as "latest" (None when there are no copies) and how many there are as "count"."""
    copies = sorted(backup_dir.glob(COPIES))
    return {"latest": copies[-1].stem.removeprefix("money-") if copies else None, "count": len(copies)}
