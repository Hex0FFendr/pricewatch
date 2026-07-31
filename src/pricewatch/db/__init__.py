"""SQLite connection management and forward-only schema migrations.

Migrations are numbered `.sql` files in `migrations/`, applied in order and
recorded in `schema_migrations`. Each is applied inside a single transaction
together with its own bookkeeping row, so a migration either lands completely
or not at all — SQLite makes DDL transactional, which is what allows this.

Applied migrations are checksummed. Editing a migration that has already run is
a mistake that produces a database whose contents disagree with its recorded
schema version, so it is detected and refused rather than ignored.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from pricewatch.clock import utcnow_iso
from pricewatch.errors import MigrationError

#: Enforced on every migration filename. The runner interpolates the version,
#: name and checksum into SQL text (parameters are not available inside
#: `executescript`), so the shape of these values is constrained, not trusted.
_MIGRATION_FILENAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    """Open the database with the pragmas this application depends on.

    `isolation_level=None` disables the sqlite3 module's implicit transaction
    handling; transactions are managed explicitly via `transaction()` and by the
    migration runner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the CLI read while the daemon writes.
    conn.execute("PRAGMA journal_mode = WAL")
    # Must be set outside a transaction, and is off by default in SQLite.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _load_migrations() -> list[Migration]:
    root = files("pricewatch.db") / "migrations"
    found: list[Migration] = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_FILENAME.match(entry.name)
        if match is None:
            raise MigrationError(
                f"migration filename {entry.name!r} does not match NNN_lower_snake.sql"
            )
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=entry.read_text(encoding="utf-8"),
            )
        )

    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration version numbers: {versions}")
    return found


def _applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    return {int(r["version"]): (str(r["name"]), str(r["checksum"])) for r in rows}


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 on an empty database."""
    conn.execute(_SCHEMA_MIGRATIONS_DDL)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"])


def migrate(conn: sqlite3.Connection) -> list[Migration]:
    """Apply every pending migration. Returns the ones applied, in order."""
    conn.execute(_SCHEMA_MIGRATIONS_DDL)

    available = _load_migrations()
    applied = _applied(conn)

    for migration in available:
        record = applied.get(migration.version)
        if record is not None and record[1] != migration.checksum:
            raise MigrationError(
                f"migration {migration.version:03d}_{migration.name} has been modified since it "
                f"was applied (recorded checksum {record[1][:12]}…, file is "
                f"{migration.checksum[:12]}…). Add a new migration instead of editing an old one."
            )

    orphans = sorted(set(applied) - {m.version for m in available})
    if orphans:
        raise MigrationError(
            f"database records migration(s) {orphans} that this build does not ship; "
            "the database is newer than the code"
        )

    pending = [m for m in available if m.version not in applied]
    for migration in pending:
        _apply(conn, migration)
    return pending


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    checksum = migration.checksum
    # Defence in depth for the interpolation below. Both values are derived, not
    # user-supplied, but this is SQL being built by string concatenation.
    if not _CHECKSUM.match(checksum) or not migration.name.replace("_", "").isalnum():
        raise MigrationError(f"refusing to apply migration with unsafe metadata: {migration.name}")

    # Interpolated rather than parameterised because `executescript` accepts no
    # parameters, and the bookkeeping row has to be inside the same script to be
    # in the same transaction. Every interpolated value is validated above:
    # version is an int, name matched `[a-z0-9_]+`, checksum is 64 hex chars.
    bookkeeping = (
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) "  # noqa: S608
        f"VALUES ({migration.version:d}, '{migration.name}', '{checksum}', '{utcnow_iso()}');"
    )
    # `executescript` issues an implicit COMMIT of any pending transaction
    # before it runs, so the transaction has to live inside the script text
    # rather than around the call.
    script = f"BEGIN IMMEDIATE;\n{migration.sql}\n{bookkeeping}\nCOMMIT;"

    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        # The script's own COMMIT never ran, so SQLite has already unwound the
        # DDL; this just clears the connection's transaction state if the
        # failure left one open.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"migration {migration.version:03d}_{migration.name} failed: {exc}"
        ) from exc


def open_database(path: Path, *, migrate_to_latest: bool = True) -> sqlite3.Connection:
    """Connect and, by default, bring the schema up to date."""
    conn = connect(path)
    if migrate_to_latest:
        migrate(conn)
    return conn
