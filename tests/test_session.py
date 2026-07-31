from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pricewatch.redaction import redact
from pricewatch.session import (
    KEY_ENV_VAR,
    ProfileBusyError,
    SessionCrypto,
    SessionCryptoError,
    SessionNotFoundError,
    SessionStore,
    profile_lock,
)
from pricewatch.session.store import StoredSession

STORAGE_STATE: dict[str, Any] = {
    "cookies": [
        {
            "name": "sid",
            "value": "a-very-secret-session-value",
            "domain": ".example.co.uk",
            "path": "/",
            "expires": 4102444800.0,  # 2100-01-01
        },
        {
            "name": "csrf",
            "value": "another-secret",
            "domain": ".example.co.uk",
            "path": "/",
            "expires": -1,  # session cookie
        },
    ],
    "origins": [
        {
            "origin": "https://www.example.co.uk",
            "localStorage": [
                {"name": "auth.bearer", "value": "eyJhbGciOiJIUzI1NiJ9.x.y"},
                {"name": "ui.theme", "value": "dark"},
            ],
        }
    ],
}


class TestCrypto:
    def test_round_trip(self, tmp_path: Path) -> None:
        crypto = SessionCrypto.load_or_create(tmp_path)
        assert crypto.decrypt(crypto.encrypt(b"hello")) == b"hello"

    def test_key_file_is_created_private(self, tmp_path: Path) -> None:
        SessionCrypto.load_or_create(tmp_path)
        mode = (tmp_path / "session.key").stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), stat.filemode(mode)

    def test_key_file_is_reused(self, tmp_path: Path) -> None:
        first = SessionCrypto.load_or_create(tmp_path)
        token = first.encrypt(b"payload")
        assert SessionCrypto.load_or_create(tmp_path).decrypt(token) == b"payload"

    def test_loose_permissions_are_refused(self, tmp_path: Path) -> None:
        SessionCrypto.load_or_create(tmp_path)
        key_path = tmp_path / "session.key"
        key_path.chmod(0o644)
        with pytest.raises(SessionCryptoError, match="accessible to group or others"):
            SessionCrypto.load_or_create(tmp_path)

    def test_env_key_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv(KEY_ENV_VAR, key)
        crypto = SessionCrypto.load_or_create(tmp_path)
        assert crypto.source == f"${KEY_ENV_VAR}"
        # No key file is written when the environment supplies one.
        assert not (tmp_path / "session.key").exists()

    def test_invalid_env_key_is_reported_clearly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY_ENV_VAR, "not-a-fernet-key")
        with pytest.raises(SessionCryptoError, match="not a valid Fernet key"):
            SessionCrypto.load_or_create(tmp_path)

    def test_wrong_key_gives_an_actionable_error(self, tmp_path: Path) -> None:
        from cryptography.fernet import Fernet

        token = SessionCrypto(Fernet.generate_key(), source="a").encrypt(b"x")
        other = SessionCrypto(Fernet.generate_key(), source="b")
        with pytest.raises(SessionCryptoError, match="pricewatch login"):
            other.decrypt(token)

    def test_source_never_exposes_the_key(self, tmp_path: Path) -> None:
        crypto = SessionCrypto.load_or_create(tmp_path)
        key = (tmp_path / "session.key").read_bytes().decode()
        assert key not in crypto.source
        assert key not in repr(crypto)


class TestStore:
    def test_save_then_load_round_trips(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        loaded = store.load("asos-uk")
        assert loaded.account == "asos-uk"
        assert loaded.storage_state == STORAGE_STATE

    def test_session_file_is_encrypted_on_disk(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        on_disk = store.session_path("asos-uk").read_bytes()
        assert b"a-very-secret-session-value" not in on_disk
        assert b"auth.bearer" not in on_disk

    def test_session_file_is_private(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        mode = store.session_path("asos-uk").stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), stat.filemode(mode)

    def test_no_temp_file_is_left_behind(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        assert list(store.sessions_dir.glob("*.tmp")) == []

    def test_missing_session_names_the_fix(self, store: SessionStore) -> None:
        with pytest.raises(SessionNotFoundError, match="pricewatch login asos-uk"):
            store.load("asos-uk")

    def test_try_load_returns_none(self, store: SessionStore) -> None:
        assert store.try_load("asos-uk") is None

    def test_overwrite_replaces_cleanly(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        store.save("asos-uk", {"cookies": [], "origins": []})
        assert store.load("asos-uk").cookies == []

    def test_delete_removes_session_and_profile(self, store: SessionStore) -> None:
        store.save("asos-uk", STORAGE_STATE)
        profile = store.profile_dir("asos-uk")
        (profile / "nested").mkdir()
        (profile / "nested" / "Cookies").write_bytes(b"chromium state")

        store.delete("asos-uk")
        assert not store.session_path("asos-uk").exists()
        assert not profile.exists()

    def test_delete_is_safe_when_nothing_exists(self, store: SessionStore) -> None:
        store.delete("never-existed")


class TestStoredSessionIntrospection:
    def session(self, **overrides: Any) -> StoredSession:
        return StoredSession(
            account="asos-uk",
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            storage_state=overrides.get("storage_state", STORAGE_STATE),
        )

    def test_cookie_and_storage_names(self) -> None:
        session = self.session()
        assert session.cookie_names == ["csrf", "sid"]
        assert session.local_storage_keys == ["auth.bearer", "ui.theme"]
        assert session.origin_urls == ["https://www.example.co.uk"]

    def test_session_cookies_are_excluded_from_expiry(self) -> None:
        # `csrf` has expires -1 and must not be treated as an expiry date.
        session = self.session()
        assert [c["name"] for c in session.persistent_cookies()] == ["sid"]
        expiry = session.earliest_expiry()
        assert expiry is not None
        assert expiry.year == 2100

    def test_summary_contains_no_values(self) -> None:
        summary = json.dumps(self.session().summary())
        assert "a-very-secret-session-value" not in summary
        assert "eyJhbGciOiJIUzI1NiJ9" not in summary
        # But it does carry the facts that make a session debuggable.
        assert "auth.bearer" in summary
        assert "sid" in summary

    def test_summary_survives_the_log_redactor(self) -> None:
        # summary() exists to be logged, so it has to come out the far side of
        # redaction still readable. Asserting on summary() alone would miss the
        # over-redaction that makes the real log line a row of [REDACTED].
        logged = redact(self.session().summary())

        assert logged["cookie_names"] == ["csrf", "sid"]
        assert logged["local_storage_keys"] == ["auth.bearer", "ui.theme"]
        assert logged["cookie_count"] == 2
        assert logged["session_captured_at"].startswith("2026-01-01")
        assert logged["session_expires_at"] is not None

    def test_a_credential_under_an_allowlisted_key_is_still_scrubbed(self) -> None:
        # Allowlisting only disables the key-based rule; the value-based rules
        # still run, so the exemption cannot become a leak.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        assert jwt not in json.dumps(redact({"cookie_names": [jwt]}))

    def test_stale_when_every_persistent_cookie_has_expired(self) -> None:
        past = (datetime.now(tz=UTC) - timedelta(days=1)).timestamp()
        state = {"cookies": [{"name": "sid", "expires": past}], "origins": []}
        assert self.session(storage_state=state).is_probably_stale()

    def test_not_stale_when_one_cookie_survives(self) -> None:
        past = (datetime.now(tz=UTC) - timedelta(days=1)).timestamp()
        future = (datetime.now(tz=UTC) + timedelta(days=1)).timestamp()
        state = {
            "cookies": [{"name": "old", "expires": past}, {"name": "new", "expires": future}],
            "origins": [],
        }
        assert not self.session(storage_state=state).is_probably_stale()

    def test_session_cookies_only_is_never_called_stale(self) -> None:
        # No expiry information is not evidence of expiry.
        state = {"cookies": [{"name": "sid", "expires": -1}], "origins": []}
        assert not self.session(storage_state=state).is_probably_stale()

    def test_missing_keys_do_not_explode(self) -> None:
        session = self.session(storage_state={})
        assert session.cookies == []
        assert session.local_storage_keys == []
        assert session.earliest_expiry() is None


class TestProfileLock:
    def test_lock_is_exclusive_across_processes(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "asos-uk.lock"
        entered = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with profile_lock(lock_path, owner="daemon"):
                entered.set()
                release.wait(timeout=10)

        thread = threading.Thread(target=holder)
        thread.start()
        assert entered.wait(timeout=5)

        # flock is per-open-file-description, so a second acquisition in this
        # process still contends with the first.
        try:
            with pytest.raises(ProfileBusyError, match="in use by"):
                with profile_lock(lock_path, owner="login"):
                    pass
        finally:
            release.set()
            thread.join(timeout=5)

    def test_error_names_the_holder(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "asos-uk.lock"
        entered = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with profile_lock(lock_path, owner="daemon-poll"):
                entered.set()
                release.wait(timeout=10)

        thread = threading.Thread(target=holder)
        thread.start()
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(ProfileBusyError) as excinfo:
                with profile_lock(lock_path, owner="login"):
                    pass
            assert "daemon-poll" in str(excinfo.value)
            assert f"pid={os.getpid()}" in str(excinfo.value)
        finally:
            release.set()
            thread.join(timeout=5)

    def test_lock_is_released_after_the_block(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "asos-uk.lock"
        with profile_lock(lock_path, owner="login"):
            pass
        with profile_lock(lock_path, owner="daemon"):
            pass

    def test_lock_is_released_on_exception(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "asos-uk.lock"
        with pytest.raises(ValueError, match="boom"), profile_lock(lock_path, owner="login"):
            raise ValueError("boom")
        with profile_lock(lock_path, owner="daemon"):
            pass

    def test_timeout_waits_for_release(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "asos-uk.lock"
        entered = threading.Event()

        def holder() -> None:
            with profile_lock(lock_path, owner="daemon"):
                entered.set()
                time.sleep(0.4)

        thread = threading.Thread(target=holder)
        thread.start()
        assert entered.wait(timeout=5)

        with profile_lock(lock_path, owner="login", timeout=5.0):
            pass
        thread.join(timeout=5)
