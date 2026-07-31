"""Network capture for `pricewatch discover`.

Records the traffic a real signed-in browsing session produces, so that a site
adapter can be written from observed fact instead of guesswork.

Redaction happens *at the moment of capture*, before an exchange is appended to
the in-memory list and long before anything is written to disk. Scrubbing on
the way out to the file would leave a window in which unredacted credentials
existed on disk; scrubbing on the way in means they never do.

JSON bodies are parsed, redacted structurally, and re-serialised rather than
being text-scrubbed. That distinction is the whole value of the capture: field
names, nesting and shapes survive intact — which is what writing an adapter
needs — while values that look like credentials do not. Prices, titles and
product ids are not credential-shaped and come through unharmed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pricewatch import __version__
from pricewatch.clock import utcnow_iso
from pricewatch.redaction import redact, redact_text

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Response

#: Only these carry a saved-items payload. Images, fonts, stylesheets, media
#: and scripts are noise and are never recorded.
RECORDED_RESOURCE_TYPES = frozenset({"xhr", "fetch", "document"})

#: JSON is what an adapter will parse, so it gets the larger budget.
MAX_JSON_BODY_BYTES = 400_000
MAX_TEXT_BODY_BYTES = 40_000
MAX_EXCHANGES = 500


@dataclass(slots=True)
class _EndpointStats:
    """Running totals for one (method, host, path) while building a summary."""

    calls: int = 0
    statuses: set[int] = field(default_factory=set)
    is_json: bool = False
    max_body_bytes: int = 0


@dataclass
class CaptureRecorder:
    """Collects redacted request/response pairs from a browser context.

    Not `slots=True`: Playwright stores a wrapper on the bound handler's
    instance when registering an event listener, which a slotted class rejects.
    """

    max_exchanges: int = MAX_EXCHANGES
    _exchanges: list[dict[str, Any]] = field(default_factory=list)
    _skipped: int = 0

    @property
    def exchanges(self) -> list[dict[str, Any]]:
        return list(self._exchanges)

    @property
    def skipped(self) -> int:
        """Exchanges dropped because the cap was reached."""
        return self._skipped

    def attach(self, context: BrowserContext) -> None:
        """Start recording. Context-level, so pages opened later are covered."""
        context.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        try:
            self._record(response)
        except Exception:  # noqa: BLE001 - a capture must never break the session
            self._skipped += 1

    def _record(self, response: Response) -> None:
        request = response.request
        if request.resource_type not in RECORDED_RESOURCE_TYPES:
            return
        if len(self._exchanges) >= self.max_exchanges:
            self._skipped += 1
            return

        body, truncated, parsed_as = _read_body(response)
        exchange: dict[str, Any] = {
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": _safe_headers(request.all_headers),
            "request_body": request.post_data,
            "response_headers": _safe_headers(response.all_headers),
            "response_body": body,
            "response_body_truncated": truncated,
            "response_body_format": parsed_as,
        }
        # The one line that matters: nothing enters the list unredacted.
        self._exchanges.append(redact(exchange))

    def summary(self) -> list[dict[str, Any]]:
        """One row per distinct endpoint, for eyeballing which call to target."""
        grouped: dict[tuple[str, str, str], _EndpointStats] = {}
        for exchange in self._exchanges:
            parts = urlsplit(str(exchange["url"]))
            key = (str(exchange["method"]), parts.netloc, parts.path)
            stats = grouped.setdefault(key, _EndpointStats())
            stats.calls += 1
            stats.statuses.add(int(exchange["status"]))
            if exchange["response_body_format"] == "json":
                stats.is_json = True
            body = exchange.get("response_body") or ""
            stats.max_body_bytes = max(stats.max_body_bytes, len(str(body)))

        rows: list[dict[str, Any]] = [
            {
                "method": method,
                "host": host,
                "path": path,
                "calls": stats.calls,
                "statuses": sorted(stats.statuses),
                "json": stats.is_json,
                "max_body_bytes": stats.max_body_bytes,
            }
            for (method, host, path), stats in grouped.items()
        ]
        # Biggest JSON payloads first: the saved-items call is near the top.
        rows.sort(key=lambda r: (not r["json"], -int(r["max_body_bytes"])))
        return rows

    def write(self, path: Path, *, account: str, site: str) -> Path:
        document = {
            "pricewatch_version": __version__,
            "account": account,
            "site": site,
            "captured_at": utcnow_iso(),
            "redacted": True,
            "note": (
                "Values that look like credentials or PII were removed at capture time. "
                "JSON structure and field names are intact, which is what an adapter "
                "needs. Check this file before sharing it."
            ),
            "exchange_count": len(self._exchanges),
            "skipped": self._skipped,
            "exchanges": self._exchanges,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create private before writing: redaction is thorough, not infallible.
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=False), encoding="utf-8")
        return path


def _safe_headers(getter: Any) -> dict[str, str]:  # noqa: ANN401 - playwright callable
    try:
        headers = getter()
    except Exception:  # noqa: BLE001 - headers are best-effort context
        return {}
    return {str(k): str(v) for k, v in dict(headers).items()}


def _read_body(response: Response) -> tuple[str | None, bool, str]:
    """Return (body, truncated, format) with JSON parsed and redacted structurally."""
    content_type = (response.header_value("content-type") or "").lower()
    is_json = "json" in content_type

    try:
        raw = response.body()
    except Exception:  # noqa: BLE001 - redirects and 204s have no body
        return None, False, "none"

    if not raw:
        return None, False, "none"

    limit = MAX_JSON_BODY_BYTES if is_json else MAX_TEXT_BODY_BYTES
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")

    if is_json and not truncated:
        try:
            # Structural redaction: keys survive, credential-shaped values do not.
            return json.dumps(redact(json.loads(text)), indent=2), False, "json"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return redact_text(text), truncated, "json" if is_json else "text"
