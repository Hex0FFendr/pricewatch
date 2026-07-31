"""`pricewatch login` — capture a signed-in session for one account.

Opens a real browser against the account's persistent profile, waits for you to
sign in, then exports the resulting storage state.

Sign-in completion is confirmed by asking the site adapter who it is signed in
as, not by watching for a URL change or a page element. A redirect proves the
page navigated; only an account identity proves authentication. Where no
adapter exists yet, the confirmation falls back to you pressing Enter, and the
session is recorded as unverified so that nothing downstream mistakes it for a
checked one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pricewatch.adapters import registry
from pricewatch.browser import persistent_context
from pricewatch.clock import utcnow_iso
from pricewatch.config import AccountConfig
from pricewatch.errors import PricewatchError
from pricewatch.logging import get_logger
from pricewatch.session import SessionStore, profile_lock

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from playwright.sync_api import BrowserContext

log = get_logger(__name__)

#: How often to ask the adapter whether sign-in has completed.
PROBE_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class LoginResult:
    account: str
    identity: str | None
    verified: bool
    cookie_count: int
    local_storage_keys: int
    session_path_display: str

    @property
    def headline(self) -> str:
        if self.verified:
            return f"signed in as {self.identity}"
        return "session saved but not verified (no adapter for this site yet)"


def run_login(
    *,
    account: AccountConfig,
    store: SessionStore,
    conn: sqlite3.Connection,
    url: str | None,
    headless: bool,
    timeout_seconds: float,
    confirm: Callable[[], bool],
    lock_timeout: float = 0.0,
) -> LoginResult:
    """Drive an interactive sign-in and persist the session.

    Args:
        confirm: called when there is no adapter to verify with; returns True
            once the operator says they have finished signing in.
    """
    target = url or account.home_url
    has_adapter = registry.available(account.site)

    if headless and not has_adapter:
        raise PricewatchError(
            f"--headless needs an adapter to detect that sign-in finished, and none is "
            f"implemented for {account.site!r} yet. Run without --headless."
        )

    with (
        profile_lock(store.lock_path(account.name), owner="login", timeout=lock_timeout),
        persistent_context(store.profile_dir(account.name), headless=headless) as context,
    ):
        page = context.pages[0] if context.pages else context.new_page()
        if target:
            page.goto(target, wait_until="domcontentloaded")

        if has_adapter:
            identity = _await_identity(context, timeout_seconds=timeout_seconds, site=account.site)
        else:
            _require_operator_confirmation(confirm)
            identity = None

        storage_state = context.storage_state()

    session = store.save(account.name, dict(storage_state))
    _record_capture(conn, account.name, verified=identity is not None)

    log.info("session captured", **session.summary(), verified=identity is not None)

    return LoginResult(
        account=account.name,
        identity=identity,
        verified=identity is not None,
        cookie_count=len(session.cookies),
        local_storage_keys=len(session.local_storage_keys),
        session_path_display=str(store.session_path(account.name)),
    )


def _await_identity(context: BrowserContext, *, timeout_seconds: float, site: str) -> str:
    """Poll the adapter until it can name the signed-in account."""
    adapter = registry.get(site)
    deadline = time.monotonic() + timeout_seconds

    while True:
        identity = adapter.probe_identity(context)
        if identity:
            return identity
        if time.monotonic() >= deadline:
            raise PricewatchError(
                f"timed out after {timeout_seconds:.0f}s waiting for sign-in to complete. "
                "The browser stayed signed out, so nothing was saved."
            )
        time.sleep(PROBE_INTERVAL_SECONDS)


def _require_operator_confirmation(confirm: Callable[[], bool]) -> None:
    """Fallback when no adapter can verify: trust the operator, record as unverified."""
    if not confirm():
        raise PricewatchError("cancelled; no session was saved")


def _record_capture(conn: sqlite3.Connection, account: str, *, verified: bool) -> None:
    """Note the capture, and clear a stale needs_reauth if we verified identity."""
    now = utcnow_iso()
    if verified:
        conn.execute(
            "UPDATE accounts SET session_captured_at = ?, status = 'ok', "
            "consecutive_failures = 0, last_error = NULL WHERE name = ?",
            (now, account),
        )
    else:
        # Status is left alone: an unverified capture is not evidence of health.
        conn.execute("UPDATE accounts SET session_captured_at = ? WHERE name = ?", (now, account))
