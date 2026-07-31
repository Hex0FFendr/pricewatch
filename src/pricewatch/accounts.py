"""Account rows: reconciling configured accounts with their persisted state.

The config file is the source of truth for *which* accounts exist; the database
owns their runtime state (session health, failure counters, last success). An
account dropped from the config is disabled rather than deleted, so that its
price history survives a config edit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pricewatch.clock import utcnow_iso
from pricewatch.config import Config
from pricewatch.db import transaction


@dataclass(frozen=True, slots=True)
class AccountState:
    id: int
    name: str
    site: str
    enabled: bool
    status: str
    last_ok_at: str | None
    last_error: str | None
    consecutive_failures: int
    last_exec_mode: str | None
    tracked_items: int

    @property
    def needs_attention(self) -> bool:
        return self.enabled and self.status in {"needs_reauth", "degraded"}


def sync_accounts(conn: sqlite3.Connection, config: Config) -> None:
    """Upsert configured accounts and disable any that are no longer configured."""
    now = utcnow_iso()
    with transaction(conn):
        for account in config.accounts:
            conn.execute(
                """
                INSERT INTO accounts (name, site, enabled, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (name) DO UPDATE SET
                    site = excluded.site,
                    enabled = excluded.enabled
                """,
                (account.name, account.site, int(account.enabled), now),
            )

        configured = [a.name for a in config.accounts]
        placeholders = ",".join("?" for _ in configured)
        if configured:
            conn.execute(
                f"UPDATE accounts SET enabled = 0 WHERE name NOT IN ({placeholders})",  # noqa: S608
                configured,
            )
        else:
            conn.execute("UPDATE accounts SET enabled = 0")


def list_accounts(conn: sqlite3.Connection) -> list[AccountState]:
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.site, a.enabled, a.status, a.last_ok_at, a.last_error,
               a.consecutive_failures, a.last_exec_mode,
               (SELECT COUNT(*) FROM items i
                 WHERE i.account_id = a.id AND i.removed_at IS NULL) AS tracked_items
          FROM accounts a
         ORDER BY a.name
        """
    ).fetchall()
    return [
        AccountState(
            id=int(r["id"]),
            name=str(r["name"]),
            site=str(r["site"]),
            enabled=bool(r["enabled"]),
            status=str(r["status"]),
            last_ok_at=r["last_ok_at"],
            last_error=r["last_error"],
            consecutive_failures=int(r["consecutive_failures"]),
            last_exec_mode=r["last_exec_mode"],
            tracked_items=int(r["tracked_items"]),
        )
        for r in rows
    ]
