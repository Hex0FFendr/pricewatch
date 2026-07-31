"""Chromium lifecycle.

One entry point, `persistent_context`, which opens a Chromium against an
account's profile directory. Both `login` and `discover` use it, and so will
the poll loop's browser path.

Deliberately absent: any attempt to disguise the browser. No fingerprint
spoofing, no automation-flag stripping, no TLS impersonation. Locale and
timezone are set to UK values because the tool targets `.co.uk` storefronts and
a British locale is the honest setting for a British shopper, not a disguise.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pricewatch.errors import PricewatchError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ViewportSize

#: Point at a specific Chromium instead of the one Playwright manages. Useful
#: for a system Chrome, or when the installed browser build does not match the
#: Playwright version's expectations.
EXECUTABLE_ENV_VAR = "PRICEWATCH_CHROMIUM_PATH"

DEFAULT_LOCALE = "en-GB"
DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_VIEWPORT: ViewportSize = {"width": 1280, "height": 900}


class BrowserLaunchError(PricewatchError):
    """Chromium could not be started."""


def chromium_executable_path() -> str | None:
    """Explicit Chromium path, if the operator set one."""
    return os.environ.get(EXECUTABLE_ENV_VAR) or None


@contextmanager
def persistent_context(
    profile_dir: Path,
    *,
    headless: bool = False,
    timeout_ms: int = 30_000,
) -> Iterator[BrowserContext]:
    """Open a Chromium bound to `profile_dir`, closing it on exit.

    The caller is expected to already hold the profile lock: Chromium takes its
    own exclusive lock on the directory, and colliding here produces a far worse
    error than `ProfileBusyError`.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                executable_path=chromium_executable_path(),
                locale=DEFAULT_LOCALE,
                timezone_id=DEFAULT_TIMEZONE,
                viewport=DEFAULT_VIEWPORT,
            )
        except PlaywrightError as exc:
            raise BrowserLaunchError(_launch_hint(exc)) from exc

        context.set_default_timeout(timeout_ms)
        try:
            yield context
        finally:
            context.close()


def _launch_hint(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
    hint = (
        f"could not start Chromium: {message}\n"
        "Install the browser with `uv run playwright install chromium`, "
        f"or point ${EXECUTABLE_ENV_VAR} at an existing Chromium or Chrome binary."
    )
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        hint += (
            "\nThere is no display set. A headed browser needs one: run under "
            "`xvfb-run -a` on a headless host, or use --headless (which sites are "
            "more likely to challenge)."
        )
    return hint
