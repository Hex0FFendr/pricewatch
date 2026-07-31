"""Shared fixtures.

Browser tests run against a real Chromium and a real local HTTP server. They
are slower than the rest of the suite but they are the only way to know the
session capture actually works, so they are marked `browser` and can be
deselected with `-m "not browser"`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from pricewatch.adapters import registry
from pricewatch.adapters.base import (
    ExecMode,
    SavedItem,
    SavedItemsResult,
    SiteProfile,
)
from pricewatch.errors import AdapterError, SessionExpiredError
from pricewatch.session import SessionStore
from tests.fake_site import FakeSite

#: The container ships a Chromium whose build number does not match what this
#: Playwright release expects, so point at it explicitly.
_BUNDLED_CHROMIUM = Path("/opt/pw-browsers/chromium")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: drives a real Chromium; slow")


@pytest.fixture(scope="session", autouse=True)
def chromium_path() -> Iterator[None]:
    if _BUNDLED_CHROMIUM.exists() and not os.environ.get("PRICEWATCH_CHROMIUM_PATH"):
        os.environ["PRICEWATCH_CHROMIUM_PATH"] = str(_BUNDLED_CHROMIUM)
    yield


@pytest.fixture(scope="session")
def display() -> Iterator[str]:
    """A DISPLAY for headed browser tests, starting Xvfb if there isn't one."""
    existing = os.environ.get("DISPLAY")
    if existing:
        yield existing
        return

    if shutil.which("Xvfb") is None:
        pytest.skip("headed browser test needs a display or Xvfb")

    port = _free_display_number()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["Xvfb", f":{port}", "-screen", "0", "1280x1024x24"],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    value = f":{port}"
    os.environ["DISPLAY"] = value
    time.sleep(1.0)
    try:
        yield value
    finally:
        process.terminate()
        process.wait(timeout=5)
        os.environ.pop("DISPLAY", None)


def _free_display_number() -> int:
    for candidate in range(90, 120):
        if not Path(f"/tmp/.X11-unix/X{candidate}").exists():  # noqa: S108 - X11 convention
            return candidate
    return 99


@pytest.fixture
def fake_site() -> Iterator[FakeSite]:
    with FakeSite() as site:
        yield site


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore.open(tmp_path / "state")


class FakeSiteAdapter:
    """Adapter for the local fake site, satisfying the real Adapter protocol."""

    site = SiteProfile(site_id="fakesite", display_name="Fake Site")
    supports_http_mode = False

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.probe_calls = 0

    def _saved_items_json(self, context: object) -> dict[str, object] | None:
        from playwright.sync_api import BrowserContext

        assert isinstance(context, BrowserContext)
        response = context.request.get(f"{self.base_url}/api/saved-items")
        if response.status != 200:
            return None
        payload: dict[str, object] = response.json()
        return payload

    def probe_identity(self, context: object) -> str | None:
        self.probe_calls += 1
        payload = self._saved_items_json(context)
        if payload is None:
            return None
        identity = payload.get("customerId")
        return str(identity) if identity else None

    def fetch_via_browser(self, context: object) -> SavedItemsResult:
        payload = self._saved_items_json(context)
        if payload is None:
            raise SessionExpiredError("fake site rejected the session")

        identity = payload.get("customerId")
        if not identity:
            raise SessionExpiredError("fake site returned no customer id")

        raw_items = payload.get("savedItems")
        if not isinstance(raw_items, list):
            raise AdapterError("savedItems was not a list")

        items = tuple(
            SavedItem(
                external_id=str(entry["productId"]),
                title=str(entry["name"]),
                price=int(entry["price"]["current"]),
                currency=str(entry["price"]["currency"]),
                variant_id=str(entry.get("variantId") or ""),
                variant_label=entry.get("size"),
                rrp=entry["price"]["was"],
                in_stock=entry.get("inStock"),
                url=f"{self.base_url}{entry['url']}",
            )
            for entry in raw_items
        )
        return SavedItemsResult(identity=str(identity), items=items, exec_mode=ExecMode.BROWSER)

    def fetch_via_http(self, session: object) -> SavedItemsResult:
        raise AdapterError("fake adapter has no http mode")


@pytest.fixture
def fake_adapter(fake_site: FakeSite) -> Iterator[FakeSiteAdapter]:
    adapter = FakeSiteAdapter(fake_site.base_url)
    registry.register(adapter)
    try:
        yield adapter
    finally:
        registry.unregister(adapter.site.site_id)
