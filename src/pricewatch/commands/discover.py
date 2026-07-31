"""`pricewatch discover` — record the traffic that a saved-items page produces.

This is the step that unblocks writing a site adapter. It deliberately knows no
API paths: it opens a browser on the account's existing profile, you navigate to
your saved items, and every JSON and document exchange is recorded, redacted,
and summarised.

Nothing here is site-specific, which is the point. An adapter written from a
capture describes traffic that was actually observed on the account it will run
against, rather than an endpoint someone remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pricewatch.browser import persistent_context
from pricewatch.capture import CaptureRecorder
from pricewatch.clock import utcnow
from pricewatch.config import AccountConfig
from pricewatch.errors import PricewatchError
from pricewatch.logging import get_logger
from pricewatch.session import SessionStore, profile_lock

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import BrowserContext

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoverResult:
    account: str
    capture_path: Path
    exchange_count: int
    skipped: int
    summary: list[dict[str, Any]]


def run_discover(
    *,
    account: AccountConfig,
    store: SessionStore,
    url: str | None,
    headless: bool,
    confirm: Callable[[], bool],
    out_path: Path | None = None,
    lock_timeout: float = 0.0,
) -> DiscoverResult:
    """Open a browser, record what it fetches, and write a redacted capture."""
    if not store.exists(account.name):
        log.warning(
            "no stored session; discover will record a signed-out browse unless the "
            "browser profile is still authenticated",
            account=account.name,
        )

    target = url or account.home_url
    recorder = CaptureRecorder()

    with (
        profile_lock(store.lock_path(account.name), owner="discover", timeout=lock_timeout),
        persistent_context(store.profile_dir(account.name), headless=headless) as context,
    ):
        recorder.attach(context)
        page = context.pages[0] if context.pages else context.new_page()
        if target:
            page.goto(target, wait_until="load")

        if not confirm():
            raise PricewatchError("cancelled; no capture was written")

        # Let in-flight requests land before the context closes. Without this, a
        # confirmation given while the saved-items XHR is still open drops the
        # one exchange the capture exists to record — and reading a response
        # body from a closing context fails, so it is lost rather than partial.
        _settle(context)

    if not recorder.exchanges:
        raise PricewatchError(
            "no JSON or document responses were recorded. Navigate to your saved-items "
            "page in the browser window before confirming, so there is traffic to capture."
        )

    path = out_path or _default_path(store, account.name)
    recorder.write(path, account=account.name, site=account.site)

    log.info(
        "capture written",
        account=account.name,
        path=str(path),
        exchanges=len(recorder.exchanges),
    )

    return DiscoverResult(
        account=account.name,
        capture_path=path,
        exchange_count=len(recorder.exchanges),
        skipped=recorder.skipped,
        summary=recorder.summary(),
    )


def _settle(context: BrowserContext, timeout_ms: int = 5_000) -> None:
    """Wait for outstanding network activity, best-effort."""
    from playwright.sync_api import Error as PlaywrightError

    for page in context.pages:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightError:
            # A page that never goes idle (polling, websockets) is normal and
            # not a reason to lose the capture.
            continue


def _default_path(store: SessionStore, account: str) -> Path:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    return store.captures_dir / f"{account}-{stamp}.json"
