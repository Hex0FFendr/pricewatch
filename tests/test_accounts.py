from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pricewatch.accounts import list_accounts, sync_accounts
from pricewatch.clock import utcnow_iso
from pricewatch.config import Config
from pricewatch.db import open_database

TWO_ACCOUNTS = """
[[accounts]]
name = "asos-uk"
site = "asos"

[[accounts]]
name = "fp-uk"
site = "freepeople"
"""


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "pw.db")


def load(tmp_path: Path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return Config.load(path)


def test_accounts_are_created_from_config(db: sqlite3.Connection, tmp_path: Path) -> None:
    sync_accounts(db, load(tmp_path, TWO_ACCOUNTS))
    states = list_accounts(db)
    assert [s.name for s in states] == ["asos-uk", "fp-uk"]
    assert all(s.status == "unknown" and s.enabled for s in states)


def test_sync_is_idempotent(db: sqlite3.Connection, tmp_path: Path) -> None:
    config = load(tmp_path, TWO_ACCOUNTS)
    sync_accounts(db, config)
    sync_accounts(db, config)
    assert len(list_accounts(db)) == 2


def test_runtime_state_survives_resync(db: sqlite3.Connection, tmp_path: Path) -> None:
    # Re-running sync must not reset session health; only the config file's
    # own fields are the config's to own.
    config = load(tmp_path, TWO_ACCOUNTS)
    sync_accounts(db, config)
    db.execute(
        "UPDATE accounts SET status = 'needs_reauth', consecutive_failures = 3, "
        "last_error = 'session expired', last_ok_at = ? WHERE name = 'asos-uk'",
        (utcnow_iso(),),
    )

    sync_accounts(db, config)
    state = next(s for s in list_accounts(db) if s.name == "asos-uk")
    assert state.status == "needs_reauth"
    assert state.consecutive_failures == 3
    assert state.last_error == "session expired"
    assert state.needs_attention


def test_removing_an_account_from_config_disables_it(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    sync_accounts(db, load(tmp_path, TWO_ACCOUNTS))
    sync_accounts(db, load(tmp_path, '[[accounts]]\nname = "asos-uk"\nsite = "asos"\n'))

    states = {s.name: s for s in list_accounts(db)}
    assert states["asos-uk"].enabled
    # Disabled, not deleted: price history outlives a config edit.
    assert not states["fp-uk"].enabled
    assert len(states) == 2


def test_disabling_in_config_propagates(db: sqlite3.Connection, tmp_path: Path) -> None:
    sync_accounts(db, load(tmp_path, TWO_ACCOUNTS))
    sync_accounts(
        db, load(tmp_path, TWO_ACCOUNTS.replace('site = "asos"', 'site = "asos"\nenabled = false'))
    )
    assert not next(s for s in list_accounts(db) if s.name == "asos-uk").enabled


def test_tracked_item_count_ignores_removed_items(db: sqlite3.Connection, tmp_path: Path) -> None:
    sync_accounts(db, load(tmp_path, TWO_ACCOUNTS))
    account_id = next(s for s in list_accounts(db) if s.name == "asos-uk").id
    now = utcnow_iso()
    db.execute(
        "INSERT INTO items (account_id, external_id, title, first_seen_at, last_seen_at) "
        "VALUES (?, 'a', 't', ?, ?)",
        (account_id, now, now),
    )
    db.execute(
        "INSERT INTO items (account_id, external_id, title, first_seen_at, last_seen_at, "
        "removed_at) VALUES (?, 'b', 't', ?, ?, ?)",
        (account_id, now, now, now),
    )
    assert next(s for s in list_accounts(db) if s.name == "asos-uk").tracked_items == 1


def test_empty_config_disables_all(db: sqlite3.Connection, tmp_path: Path) -> None:
    sync_accounts(db, load(tmp_path, TWO_ACCOUNTS))
    sync_accounts(db, load(tmp_path, "[general]\n"))
    assert all(not s.enabled for s in list_accounts(db))
