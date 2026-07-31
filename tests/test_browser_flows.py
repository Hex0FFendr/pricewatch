"""End-to-end tests for login, discover and adapter fetch.

These drive a real Chromium against a real local HTTP server. Mocking
Playwright here would only prove the mocks agree with themselves; the whole
point is to know that cookies survive a profile, that storage_state exports
what we think it does, and that the capture recorder redacts real traffic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pricewatch.adapters import registry
from pricewatch.browser import persistent_context
from pricewatch.commands.discover import run_discover
from pricewatch.commands.login import run_login
from pricewatch.config import AccountConfig
from pricewatch.db import open_database
from pricewatch.errors import PricewatchError, SessionExpiredError
from pricewatch.session import SessionStore
from tests.conftest import FakeSiteAdapter
from tests.fake_site import BEARER_TOKEN, CUSTOMER_ID, SESSION_COOKIE, SESSION_TOKEN, FakeSite

pytestmark = pytest.mark.browser


def account_for(site: str = "fakesite") -> AccountConfig:
    # `site` is a Literal on AccountConfig, so build the fake through validation
    # rather than widening the production type for tests.
    return AccountConfig.model_construct(name="test-acct", site=site, enabled=True, home_url=None)


def sign_in(profile_dir: Path, base_url: str) -> None:
    """Do what a human does in the headed window: submit the sign-in form."""
    with persistent_context(profile_dir, headless=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{base_url}/", wait_until="domcontentloaded")
        page.click("#signin")
        page.wait_for_url("**/account")


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_database(tmp_path / "pw.db")
    conn.execute(
        "INSERT INTO accounts (name, site, created_at) "
        "VALUES ('test-acct', 'fakesite', '2026-01-01')"
    )
    return conn


class TestLogin:
    def test_captures_a_verified_session(
        self,
        fake_site: FakeSite,
        fake_adapter: FakeSiteAdapter,
        store: SessionStore,
        db: sqlite3.Connection,
    ) -> None:
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)

        result = run_login(
            account=account_for(),
            store=store,
            conn=db,
            url=f"{fake_site.base_url}/account",
            headless=True,
            timeout_seconds=20,
            confirm=lambda: pytest.fail("adapter should verify without asking"),
        )

        assert result.verified
        assert result.identity == CUSTOMER_ID
        assert fake_adapter.probe_calls >= 1

    def test_exports_cookies_and_local_storage(
        self,
        fake_site: FakeSite,
        fake_adapter: FakeSiteAdapter,
        store: SessionStore,
        db: sqlite3.Connection,
    ) -> None:
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        run_login(
            account=account_for(),
            store=store,
            conn=db,
            url=f"{fake_site.base_url}/account",
            headless=True,
            timeout_seconds=20,
            confirm=lambda: True,
        )

        session = store.load("test-acct")
        assert SESSION_COOKIE in session.cookie_names
        # localStorage is where a bearer token lives, and storage_state carries
        # it. This is the fact the HTTP fast path would depend on.
        assert "auth.bearer" in session.local_storage_keys

    def test_marks_the_account_healthy_on_a_verified_login(
        self,
        fake_site: FakeSite,
        fake_adapter: FakeSiteAdapter,
        store: SessionStore,
        db: sqlite3.Connection,
    ) -> None:
        db.execute(
            "UPDATE accounts SET status = 'needs_reauth', consecutive_failures = 4, "
            "last_error = 'expired' WHERE name = 'test-acct'"
        )
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        run_login(
            account=account_for(),
            store=store,
            conn=db,
            url=f"{fake_site.base_url}/account",
            headless=True,
            timeout_seconds=20,
            confirm=lambda: True,
        )

        row = db.execute(
            "SELECT status, consecutive_failures, last_error, session_captured_at "
            "FROM accounts WHERE name = 'test-acct'"
        ).fetchone()
        assert row["status"] == "ok"
        assert row["consecutive_failures"] == 0
        assert row["last_error"] is None
        assert row["session_captured_at"] is not None

    def test_times_out_when_sign_in_never_happens(
        self,
        fake_site: FakeSite,
        fake_adapter: FakeSiteAdapter,
        store: SessionStore,
        db: sqlite3.Connection,
    ) -> None:
        # Fresh profile, never signed in: the probe keeps returning None.
        with pytest.raises(PricewatchError, match="timed out"):
            run_login(
                account=account_for(),
                store=store,
                conn=db,
                url=f"{fake_site.base_url}/",
                headless=True,
                timeout_seconds=3,
                confirm=lambda: True,
            )
        # Nothing was saved, so a later run cannot mistake this for a session.
        assert not store.exists("test-acct")

    def test_unverified_login_leaves_status_alone(
        self, fake_site: FakeSite, store: SessionStore, db: sqlite3.Connection, display: str
    ) -> None:
        # No adapter registered: the operator confirms, and the capture is
        # recorded but must not be treated as evidence of health.
        db.execute("UPDATE accounts SET status = 'needs_reauth' WHERE name = 'test-acct'")
        result = run_login(
            account=account_for(),
            store=store,
            conn=db,
            url=f"{fake_site.base_url}/",
            headless=False,
            timeout_seconds=20,
            confirm=lambda: True,
        )

        assert not result.verified
        assert result.identity is None
        row = db.execute(
            "SELECT status, session_captured_at FROM accounts WHERE name = 'test-acct'"
        ).fetchone()
        assert row["status"] == "needs_reauth"
        assert row["session_captured_at"] is not None

    def test_declining_the_confirmation_saves_nothing(
        self, fake_site: FakeSite, store: SessionStore, db: sqlite3.Connection, display: str
    ) -> None:
        with pytest.raises(PricewatchError, match="cancelled"):
            run_login(
                account=account_for(),
                store=store,
                conn=db,
                url=f"{fake_site.base_url}/",
                headless=False,
                timeout_seconds=20,
                confirm=lambda: False,
            )
        assert not store.exists("test-acct")

    def test_headless_without_an_adapter_is_refused(
        self, store: SessionStore, db: sqlite3.Connection
    ) -> None:
        # Nobody could see the window to sign in, and nothing could detect that
        # they had. Refuse rather than hang. No browser is launched.
        with pytest.raises(PricewatchError, match="--headless needs an adapter"):
            run_login(
                account=account_for(),
                store=store,
                conn=db,
                url=None,
                headless=True,
                timeout_seconds=5,
                confirm=lambda: True,
            )


class TestDiscover:
    def _capture(self, fake_site: FakeSite, store: SessionStore) -> dict[str, object]:
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        result = run_discover(
            account=account_for(),
            store=store,
            url=f"{fake_site.base_url}/account",
            headless=True,
            confirm=lambda: True,
        )
        document: dict[str, object] = json.loads(result.capture_path.read_text())
        return document

    def test_records_the_json_endpoint(self, fake_site: FakeSite, store: SessionStore) -> None:
        document = self._capture(fake_site, store)
        exchanges = document["exchanges"]
        assert isinstance(exchanges, list)
        paths = {str(e["url"]).split("?")[0] for e in exchanges}
        assert f"{fake_site.base_url}/api/saved-items" in paths

    def test_ignores_non_api_resource_types(self, fake_site: FakeSite, store: SessionStore) -> None:
        document = self._capture(fake_site, store)
        exchanges = document["exchanges"]
        assert isinstance(exchanges, list)
        assert all("app.css" not in str(e["url"]) for e in exchanges)

    def test_credentials_never_reach_the_capture_file(
        self, fake_site: FakeSite, store: SessionStore
    ) -> None:
        text = json.dumps(self._capture(fake_site, store))
        assert SESSION_TOKEN not in text
        assert BEARER_TOKEN not in text
        assert "shopper@example.co.uk" not in text

    def test_json_structure_survives_redaction(
        self, fake_site: FakeSite, store: SessionStore
    ) -> None:
        # The reason to redact structurally rather than by text-scrubbing: an
        # adapter is written from field names and shapes, so those must survive.
        document = self._capture(fake_site, store)
        exchanges = document["exchanges"]
        assert isinstance(exchanges, list)
        saved = next(e for e in exchanges if "/api/saved-items" in str(e["url"]))
        body = json.loads(str(saved["response_body"]))

        assert body["customerId"] == CUSTOMER_ID  # identity field is not a secret
        assert len(body["savedItems"]) == 2
        first = body["savedItems"][0]
        assert first["productId"] == "12345678"
        assert first["name"] == "Midi Tea Dress"
        assert first["price"] == {"current": 2999, "was": 5999, "currency": "GBP"}
        assert first["inStock"] is True
        # ...while the credential in the same payload is gone.
        assert body["sessionToken"] == "[REDACTED]"

    def test_capture_file_is_private(self, fake_site: FakeSite, store: SessionStore) -> None:
        import stat

        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        result = run_discover(
            account=account_for(),
            store=store,
            url=f"{fake_site.base_url}/account",
            headless=True,
            confirm=lambda: True,
        )
        mode = result.capture_path.stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), stat.filemode(mode)

    def test_summary_puts_the_json_endpoint_first(
        self, fake_site: FakeSite, store: SessionStore
    ) -> None:
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        result = run_discover(
            account=account_for(),
            store=store,
            url=f"{fake_site.base_url}/account",
            headless=True,
            confirm=lambda: True,
        )
        assert result.summary[0]["path"] == "/api/saved-items"
        assert result.summary[0]["json"] is True

    def test_declining_writes_nothing(self, fake_site: FakeSite, store: SessionStore) -> None:
        with pytest.raises(PricewatchError, match="cancelled"):
            run_discover(
                account=account_for(),
                store=store,
                url=f"{fake_site.base_url}/account",
                headless=True,
                confirm=lambda: False,
            )
        assert not store.captures_dir.exists() or list(store.captures_dir.iterdir()) == []


class TestAdapterFetch:
    def test_returns_normalised_items(
        self, fake_site: FakeSite, fake_adapter: FakeSiteAdapter, store: SessionStore
    ) -> None:
        sign_in(store.profile_dir("test-acct"), fake_site.base_url)
        with persistent_context(store.profile_dir("test-acct"), headless=True) as context:
            result = fake_adapter.fetch_via_browser(context)

        assert result.identity == CUSTOMER_ID
        assert not result.is_empty
        dress = result.items[0]
        assert dress.external_id == "12345678"
        assert dress.price == 2999
        assert dress.rrp == 5999
        assert dress.variant_id == "uk-10"
        assert dress.in_stock is True
        assert result.items[1].in_stock is False

    def test_empty_saved_list_is_not_session_death(
        self, fake_adapter: FakeSiteAdapter, store: SessionStore
    ) -> None:
        # The hole in the original design: an empty list is a legitimate state,
        # and it is distinguishable because identity still resolves.
        with FakeSite(empty_list=True) as site:
            registry.unregister("fakesite")
            adapter = FakeSiteAdapter(site.base_url)
            registry.register(adapter)
            try:
                sign_in(store.profile_dir("empty-acct"), site.base_url)
                with persistent_context(store.profile_dir("empty-acct"), headless=True) as context:
                    result = adapter.fetch_via_browser(context)
            finally:
                registry.unregister("fakesite")

        assert result.is_empty
        assert result.identity == CUSTOMER_ID

    def test_rejected_session_raises_session_expired(self, store: SessionStore) -> None:
        with FakeSite(reject_session=True) as site:
            adapter = FakeSiteAdapter(site.base_url)
            sign_in(store.profile_dir("dead-acct"), site.base_url)
            with (
                persistent_context(store.profile_dir("dead-acct"), headless=True) as context,
                pytest.raises(SessionExpiredError),
            ):
                adapter.fetch_via_browser(context)

    def test_probe_returns_none_when_signed_out(
        self, fake_site: FakeSite, fake_adapter: FakeSiteAdapter, store: SessionStore
    ) -> None:
        # Never signed in. Must return None, not raise: "signed out" is a normal
        # answer during a login wait.
        with persistent_context(store.profile_dir("fresh"), headless=True) as context:
            assert fake_adapter.probe_identity(context) is None


class TestProfilePersistence:
    def test_a_session_survives_across_browser_launches(
        self, fake_site: FakeSite, store: SessionStore
    ) -> None:
        # This is why the profile directory is the source of truth rather than
        # the exported storage_state.
        profile = store.profile_dir("test-acct")
        sign_in(profile, fake_site.base_url)

        with persistent_context(profile, headless=True) as context:
            response = context.request.get(f"{fake_site.base_url}/api/saved-items")
            assert response.status == 200
            assert response.json()["customerId"] == CUSTOMER_ID

    def test_a_different_profile_is_signed_out(
        self, fake_site: FakeSite, store: SessionStore
    ) -> None:
        with persistent_context(store.profile_dir("other"), headless=True) as context:
            assert context.request.get(f"{fake_site.base_url}/api/saved-items").status == 401
