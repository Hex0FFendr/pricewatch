"""Encryption for exported session state.

Scope, stated plainly because it is easy to overestimate: this protects the
*exported* session file (`sessions/<account>.json.enc`). The browser profile
directory sitting beside it holds the same cookies in Chromium's own stores and
is not encrypted, because Chromium owns that format and needs it readable.

So this defends against session state leaking through a backup, a synced
directory, or a casual `cat`. It does not defend against someone who can read
the state directory as your user — they can read the profile instead. The
honest summary is "partial", and the README says so.

The key comes from `PRICEWATCH_SESSION_KEY` if set, otherwise a generated key
file with 0600 permissions. A passphrase prompt is deliberately not offered:
the daemon has to survive an unattended restart.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from pricewatch.errors import PricewatchError

#: Environment variable holding a urlsafe-base64 32-byte Fernet key.
KEY_ENV_VAR = "PRICEWATCH_SESSION_KEY"
KEY_FILENAME = "session.key"


class SessionCryptoError(PricewatchError):
    """The session key is unusable, or a session file could not be decrypted."""


class SessionCrypto:
    """Encrypts and decrypts session blobs with a single symmetric key."""

    __slots__ = ("_fernet", "_source")

    def __init__(self, key: bytes, *, source: str) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise SessionCryptoError(
                f"session key from {source} is not a valid Fernet key "
                f"(expected urlsafe-base64 of 32 bytes): {exc}"
            ) from exc
        self._source = source

    @property
    def source(self) -> str:
        """Where the key came from, for logging. Never the key itself."""
        return self._source

    @classmethod
    def load_or_create(cls, state_dir: Path) -> SessionCrypto:
        env_key = os.environ.get(KEY_ENV_VAR)
        if env_key:
            return cls(env_key.encode("ascii"), source=f"${KEY_ENV_VAR}")

        key_path = state_dir / KEY_FILENAME
        if key_path.exists():
            _require_private(key_path)
            return cls(key_path.read_bytes().strip(), source=str(key_path))

        state_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Create with restrictive permissions from the start rather than
        # chmod-ing afterwards, which would leave the key briefly world-readable.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return cls(key, source=str(key_path))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise SessionCryptoError(
                "could not decrypt the stored session: the key does not match the file. "
                f"If {KEY_ENV_VAR} was set or changed since the session was saved, restore "
                "the original key or sign in again with `pricewatch login`."
            ) from exc


def _require_private(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SessionCryptoError(
            f"session key {path} is accessible to group or others "
            f"(mode {stat.filemode(mode)}); run: chmod 600 {path}"
        )
