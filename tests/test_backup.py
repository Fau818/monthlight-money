import sqlite3
from datetime import date

from app import backup
from app.db import connect


def make_db(path):
    conn = connect(path)
    conn.execute("INSERT INTO meta (key, value) VALUES ('check', 'kept')")
    conn.commit()
    return conn


def test_daily_backup_makes_one_complete_copy_a_day(tmp_path):
    db_path, backups = tmp_path / "data" / "money.db", tmp_path / "backups"
    live = make_db(db_path)  # left open, like the running app's connection

    made = backup.make_daily_backup(db_path, backups, date(2026, 10, 14))

    assert made == backups / "money-2026-10-14.db"
    assert backup.make_daily_backup(db_path, backups, date(2026, 10, 14)) is None
    assert [p.name for p in backups.iterdir()] == ["money-2026-10-14.db"]
    assert list(db_path.parent.glob("backup-in-progress*")) == []
    copy = sqlite3.connect(made)
    assert copy.execute("SELECT value FROM meta WHERE key = 'check'").fetchone()[0] == "kept"
    copy.close()
    live.close()


def test_daily_backup_keeps_only_the_newest_copies(tmp_path):
    db_path, backups = tmp_path / "data" / "money.db", tmp_path / "backups"
    make_db(db_path).close()
    backups.mkdir()
    (backups / "notes.txt").write_text("not a backup")

    for day in range(1, 5):
        backup.make_daily_backup(db_path, backups, date(2026, 10, day), keep=2)

    assert sorted(p.name for p in backups.iterdir()) == ["money-2026-10-03.db", "money-2026-10-04.db", "notes.txt"]
    assert backup.backup_summary(backups) == {"latest": "2026-10-04", "count": 2}


def test_backup_summary_without_backups(tmp_path):
    assert backup.backup_summary(tmp_path / "missing") == {"latest": None, "count": 0}


def test_backups_go_on_with_automatic_syncing_off(settings, monkeypatch):
    from app import main, sync
    connect(settings.db_path).close()
    monkeypatch.setattr(sync, "run_sync", lambda *args: (_ for _ in ()).throw(AssertionError("synced")))
    assert settings.sync_interval_hours == 0
    main.background_work(settings)
    assert [p.name for p in settings.backup_dir.iterdir()] == ["money-2026-10-14.db"]  # today, as the tests fix it
