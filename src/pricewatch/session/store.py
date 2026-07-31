"""Persistence for browser profiles and exported session state.

Two artefacts per account, and the direction of flow between them matters:

* `profiles/<account>/` — a Chromium persistent profile. This is the source of
  truth. Keeping the whole profile, rather than just cookies, preserves the
  browser-side state a site associates with the session, which is what makes a
  session survive.

* `sessions/<account>.json.enc` — an encrypted export of Playwright's
  `storage_state` (cookies plus localStorage), *derived* from the profile.
  This is what an HTTP fast path would replay.

They are alternatives, not partners: `launch_persistent_context` accepts no
`storage_state` argument. Nothing loads the export back into a browser; it is
written on login and read only by the HTTP path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pricewatch.clock import utcnow
from pricewatch.errors import PricewatchError
from pricewatch.session.crypto import SessionCrypto

#: Playwright's marker for a session cookie (dies with the browser).
SESSION_COOKIE_EXPIRY = -1


class SessionNotFoundError(PricewatchError):
    """No session has been captured for this account yet."""


@dataclass(frozen=True, slots=True)
class StoredSession:
    """An exported, decrypted session."""

    account: str
    captured_at: datetime
    storage_state: dict[str, Any]

    @property
    def cookies(self) -> list[dict[str, Any]]:
        raw = self.storage_state.get("cookies", [])
        return list(raw) if isinstance(raw, list) else []

    @property
    def origins(self) -> list[dict[str, Any]]:
        """localStorage entries, per origin. Where bearer tokens usually live."""
        raw = self.storage_state.get("origins", [])
        return list(raw) if isinstance(raw, list) else []

    @property
    def cookie_names(self) -> list[str]:
        return sorted(str(c.get("name", "")) for c in self.cookies)

    @property
    def origin_urls(self) -> list[str]:
        return sorted(str(o.get("origin", "")) for o in self.origins)

    @property
    def local_storage_keys(self) -> list[str]:
        keys: list[str] = []
        for origin in self.origins:
            entries = origin.get("localStorage", [])
            if isinstance(entries, list):
                keys.extend(str(entry.get("name", "")) for entry in entries)
        return sorted(keys)

    def persistent_cookies(self) -> list[dict[str, Any]]:
        """Cookies with a real expiry, i.e. excluding session cookies."""
        return [c for c in self.cookies if float(c.get("expires", SESSION_COOKIE_EXPIRY)) > 0]

    def earliest_expiry(self) -> datetime | None:
        expiries = [float(c["expires"]) for c in self.persistent_cookies()]
        if not expiries:
            return None
        return datetime.fromtimestamp(min(expiries), tz=UTC)

    def expired_cookie_names(self, now: datetime | None = None) -> list[str]:
        moment = (now or utcnow()).timestamp()
        return sorted(
            str(c.get("name", ""))
            for c in self.persistent_cookies()
            if float(c["expires"]) <= moment
        )

    def is_probably_stale(self, now: datetime | None = None) -> bool:
        """Cheap pre-check: every persistent cookie has passed its expiry.

        Only ever a hint. The authoritative test is whether the adapter can
        still establish an account identity — a site can invalidate a session
        long before its cookies expire, and can equally keep one alive past
        them. This exists to skip a pointless browser launch, never to decide
        that a session is dead.
        """
        persistent = self.persistent_cookies()
        if not persistent:
            return False
        return len(self.expired_cookie_names(now)) == len(persistent)

    def summary(self) -> dict[str, Any]:
        """Facts about the session, safe to log. Contains no values."""
        expiry = self.earliest_expiry()
        return {
            "account": self.account,
            "session_captured_at": self.captured_at.isoformat(timespec="seconds"),
            "cookie_count": len(self.cookies),
            "cookie_names": self.cookie_names,
            "origins": self.origin_urls,
            "local_storage_keys": self.local_storage_keys,
            "session_expires_at": expiry.isoformat(timespec="seconds") if expiry else None,
        }


class SessionStore:
    """Filesystem layout and encrypted persistence for sessions."""

    __slots__ = ("_crypto", "_state_dir")

    def __init__(self, state_dir: Path, crypto: SessionCrypto) -> None:
        self._state_dir = state_dir
        self._crypto = crypto

    @classmethod
    def open(cls, state_dir: Path) -> SessionStore:
        return cls(state_dir, SessionCrypto.load_or_create(state_dir))

    @property
    def profiles_dir(self) -> Path:
        return self._state_dir / "profiles"

    @property
    def sessions_dir(self) -> Path:
        return self._state_dir / "sessions"

    @property
    def captures_dir(self) -> Path:
        return self._state_dir / "captures"

    def profile_dir(self, account: str) -> Path:
        path = self.profiles_dir / account
        path.mkdir(parents=True, exist_ok=True)
        return path

    def lock_path(self, account: str) -> Path:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        return self.profiles_dir / f"{account}.lock"

    def session_path(self, account: str) -> Path:
        return self.sessions_dir / f"{account}.json.enc"

    def exists(self, account: str) -> bool:
        return self.session_path(account).exists()

    def save(self, account: str, storage_state: dict[str, Any]) -> StoredSession:
        session = StoredSession(account=account, captured_at=utcnow(), storage_state=storage_state)
        envelope = {
            "account": account,
            "captured_at": session.captured_at.isoformat(),
            "storage_state": storage_state,
        }
        plaintext = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_path(account)
        # Write via a private temp file and rename, so a crash mid-write cannot
        # leave a truncated session, and the bytes are never world-readable.
        temp = path.with_suffix(".tmp")
        temp.touch(mode=0o600, exist_ok=True)
        temp.write_bytes(self._crypto.encrypt(plaintext))
        temp.replace(path)
        return session

    def load(self, account: str) -> StoredSession:
        path = self.session_path(account)
        try:
            token = path.read_bytes()
        except FileNotFoundError as exc:
            raise SessionNotFoundError(
                f"no stored session for {account!r}; run `pricewatch login {account}`"
            ) from exc

        envelope = json.loads(self._crypto.decrypt(token).decode("utf-8"))
        return StoredSession(
            account=str(envelope["account"]),
            captured_at=datetime.fromisoformat(str(envelope["captured_at"])),
            storage_state=dict(envelope["storage_state"]),
        )

    def try_load(self, account: str) -> StoredSession | None:
        try:
            return self.load(account)
        except SessionNotFoundError:
            return None

    def delete(self, account: str) -> None:
        """Remove the exported session and the profile. Used to force re-login."""
        self.session_path(account).unlink(missing_ok=True)
        profile = self.profiles_dir / account
        if profile.exists():
            _remove_tree(profile)


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink(missing_ok=True)
    path.rmdir()
