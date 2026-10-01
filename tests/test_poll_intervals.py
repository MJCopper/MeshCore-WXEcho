"""Per-service interval settings and migration from the old BOM seconds value."""
from app.db import Database
from app.config import polling_seconds


def test_default_poll_intervals_are_independent_minutes():
    db = Database(":memory:")
    assert db.get_setting("bom_enabled") is True
    assert [db.get_setting(key) for key in
            ("bom_poll_minutes", "rfs_poll_minutes", "traffic_poll_minutes")] == [5, 10, 10]
    db.set_setting("rfs_poll_minutes", 15)
    assert db.get_setting("bom_poll_minutes") == 5
    assert db.get_setting("traffic_poll_minutes") == 10
    db.close()


def test_existing_bom_seconds_migrate_up_to_minimum_and_round_up(tmp_path):
    path = tmp_path / "old-interval.db"
    db = Database(str(path))
    db.set_setting("poll_interval", 361)
    with db._lock:
        db._conn.execute("DELETE FROM settings WHERE key = 'bom_poll_minutes'")
        db._conn.commit()
    db.close()
    db = Database(str(path))
    assert db.get_setting("bom_poll_minutes") == 7
    db.close()

    db = Database(str(path))
    db.set_setting("poll_interval", 120)
    with db._lock:
        db._conn.execute("DELETE FROM settings WHERE key = 'bom_poll_minutes'")
        db._conn.commit()
    db.close()
    db = Database(str(path))
    assert db.get_setting("bom_poll_minutes") == 5
    db.close()


def test_poll_runtime_clamps_all_services_to_five_minutes():
    assert polling_seconds(1, 10) == 300
    assert polling_seconds(7, 10) == 420
    assert polling_seconds("invalid", 10) == 600
