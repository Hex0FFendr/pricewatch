"""Time helpers.

Every timestamp this application stores is UTC ISO-8601 with an explicit
offset. Centralised here so that tests have one thing to patch and so that no
naive datetime ever reaches the database.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")
