from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pricewatch.clock import utcnow_iso
from pricewatch.db import (
    Migration,
    connect,
    current_version,
    latest_available_version,
    migrate,
    open_database,
    transaction,
)
from pricewatch.errors import MigrationError


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "pricewatch.db")


class TestConnection:
    def test_pragmas_are_applied(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "pw.db")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # Foreign keys are off by default in SQLite; the schema relies on them.
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_parent_directory_is_created(self, tmp_path: Path) -> None:
        connect(tmp_path / "nested" / "deeper" / "pw.db")
        assert (tmp_path / "nested" / "deeper" / "pw.db").exists()

    def test_transaction_rolls_back_on_error(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="boom"), transaction(db):
            db.execute(
                "INSERT INTO accounts (name, site, created_at) VALUES ('x', 'asos', ?)",
                (utcnow_iso(),),
            )
            raise ValueError("boom")
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0

    def test_transaction_commits_on_success(self, db: sqlite3.Connection) -> None:
        with transaction(db):
            db.execute(
                "INSERT INTO accounts (name, site, created_at) VALUES ('x', 'asos', ?)",
                (utcnow_iso(),),
            )
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


class TestMigrations:
    def test_fresh_database_applies_everything(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "pw.db")
        assert current_version(conn) == 0
        applied = migrate(conn)
        assert [m.version for m in applied] == [m.version for m in _real_migrations()]
        assert current_version(conn) == latest_available_version()

    def test_migrating_twice_is_a_no_op(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "pw.db")
        migrate(conn)
        assert migrate(conn) == []

    def test_expected_tables_exist(self, db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        names = {r["name"] for r in rows}
        assert {
            "accounts",
            "items",
            "observations",
            "notifications",
            "notification_deliveries",
            "poll_runs",
            "raw_responses",
            "schema_migrations",
        } <= names

    def test_modified_applied_migration_is_refused(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "pw.db")
        migrate(conn)
        # Simulate someone editing 001 after it shipped.
        conn.execute("UPDATE schema_migrations SET checksum = ? WHERE version = 1", ("0" * 64,))
        with pytest.raises(MigrationError, match="modified since it was applied"):
            migrate(conn)

    def test_database_newer_than_code_is_refused(self, tmp_path: Path) -> None:
        conn = connect(tmp_path / "pw.db")
        migrate(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (99, 'from_the_future', ?, ?)",
            ("a" * 64, utcnow_iso()),
        )
        with pytest.raises(MigrationError, match="newer than the code"):
            migrate(conn)

    def test_failed_migration_leaves_no_partial_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = Migration(
            # Far beyond any real migration, so this stays valid as more ship.
            version=999,
            name="broken",
            sql="CREATE TABLE good_table (id INTEGER);\nTHIS IS NOT SQL;",
        )
        # Resolve the real set *before* patching, or the fake calls itself.
        real = _real_migrations()
        planned = [*real, broken]
        monkeypatch.setattr("pricewatch.db._load_migrations", lambda: planned)
        conn = connect(tmp_path / "pw.db")
        with pytest.raises(MigrationError, match="999_broken failed"):
            migrate(conn)

        # The first statement of the failed migration must have been rolled back.
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert "good_table" not in tables
        # The real migrations before it still applied and committed.
        assert current_version(conn) == real[-1].version

    def test_badly_named_migration_file_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Version numbers drive ordering, so a file that does not declare one
        # must stop the run rather than be applied in arbitrary position.
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "1_missing_padding.sql").write_text("SELECT 1;", encoding="utf-8")
        monkeypatch.setattr("pricewatch.db.files", lambda _: tmp_path)

        with pytest.raises(MigrationError, match="does not match"):
            migrate(connect(tmp_path / "pw.db"))

    def test_duplicate_versions_are_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "001_one.sql").write_text("SELECT 1;", encoding="utf-8")
        (migrations / "001_two.sql").write_text("SELECT 1;", encoding="utf-8")
        monkeypatch.setattr("pricewatch.db.files", lambda _: tmp_path)

        with pytest.raises(MigrationError, match="duplicate migration version"):
            migrate(connect(tmp_path / "pw.db"))


def _real_migrations() -> list[Migration]:
    from pricewatch.db import _load_migrations

    return _load_migrations()


class TestSchemaSemantics:
    """Constraints the trigger engine will rely on in phase 3."""

    def _account(self, db: sqlite3.Connection) -> int:
        cur = db.execute(
            "INSERT INTO accounts (name, site, created_at) VALUES ('asos-uk', 'asos', ?)",
            (utcnow_iso(),),
        )
        return int(cur.lastrowid or 0)

    def _item(self, db: sqlite3.Connection, account_id: int, variant: str = "") -> int:
        now = utcnow_iso()
        cur = db.execute(
            "INSERT INTO items (account_id, external_id, variant_id, title, "
            "first_seen_at, last_seen_at) VALUES (?, '12345678', ?, 'A dress', ?, ?)",
            (account_id, variant, now, now),
        )
        return int(cur.lastrowid or 0)

    def test_foreign_keys_are_enforced(self, db: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO items (account_id, external_id, title, first_seen_at, last_seen_at) "
                "VALUES (9999, 'x', 't', ?, ?)",
                (utcnow_iso(), utcnow_iso()),
            )

    def test_item_uniqueness_holds_for_the_no_variant_case(self, db: sqlite3.Connection) -> None:
        # The reason variant_id is NOT NULL DEFAULT '': a nullable column would
        # let this insert succeed twice, because SQLite treats NULLs as distinct.
        account_id = self._account(db)
        self._item(db, account_id)
        with pytest.raises(sqlite3.IntegrityError):
            self._item(db, account_id)

    def test_same_product_in_two_sizes_is_two_items(self, db: sqlite3.Connection) -> None:
        account_id = self._account(db)
        self._item(db, account_id, variant="uk-10")
        self._item(db, account_id, variant="uk-12")
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2

    def test_notification_dedupe_key_blocks_a_repeat(self, db: sqlite3.Connection) -> None:
        item_id = self._item(db, self._account(db))
        args = (item_id, "any_drop", 2999, 0, utcnow_iso())
        sql = (
            "INSERT INTO notifications (item_id, trigger_kind, price, streak_seq, created_at) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        db.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, args)

    def test_same_price_in_a_new_streak_is_notifiable(self, db: sqlite3.Connection) -> None:
        # £30 -> £45 -> £30 must alert twice; the streak counter is what allows it.
        item_id = self._item(db, self._account(db))
        sql = (
            "INSERT INTO notifications (item_id, trigger_kind, price, streak_seq, created_at) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        db.execute(sql, (item_id, "any_drop", 2999, 0, utcnow_iso()))
        db.execute(sql, (item_id, "any_drop", 2999, 1, utcnow_iso()))
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2

    def test_unknown_trigger_kind_is_rejected(self, db: sqlite3.Connection) -> None:
        item_id = self._item(db, self._account(db))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO notifications (item_id, trigger_kind, price, streak_seq, created_at) "
                "VALUES (?, 'vibes', 100, 0, ?)",
                (item_id, utcnow_iso()),
            )

    def test_deleting_an_account_cascades(self, db: sqlite3.Connection) -> None:
        account_id = self._account(db)
        self._item(db, account_id)
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_account_status_is_constrained(self, db: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO accounts (name, site, status, created_at) "
                "VALUES ('x', 'asos', 'probably_fine', ?)",
                (utcnow_iso(),),
            )
