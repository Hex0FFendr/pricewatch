"""A local retail site, for testing the browser machinery against something real.

The alternative — mocking Playwright — would test that the mocks were written
consistently. This runs an actual HTTP server and an actual Chromium, so the
session capture, the cookie export and the capture recorder are exercised end
to end. What it deliberately does *not* do is impersonate ASOS or Free People:
it is a generic cookie-gated saved-items API, which is enough to prove the
plumbing without encoding a guess about either real site.

Shape:
    GET  /                  sign-in form
    POST /signin            sets the session cookie, stores a bearer token in
                            localStorage, redirects to /account
    GET  /account           page whose script fetches the saved-items API
    GET  /api/saved-items   401 without the cookie; otherwise the payload,
                            including the customer id that proves identity
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

SESSION_COOKIE = "fake_sid"
#: Credential-shaped on purpose: tests assert this never survives into a capture.
SESSION_TOKEN = "s3ss10n" + "a1b2c3d4" * 6
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjdXN0LTk5In0.ZmFrZXNpZ25hdHVyZXZhbHVl"
CUSTOMER_ID = "cust-99"

SAVED_ITEMS: list[dict[str, Any]] = [
    {
        "productId": "12345678",
        "name": "Midi Tea Dress",
        "variantId": "uk-10",
        "size": "UK 10",
        "price": {"current": 2999, "was": 5999, "currency": "GBP"},
        "inStock": True,
        "url": "/product/12345678",
    },
    {
        "productId": "87654321",
        "name": "Wool Blend Coat",
        "variantId": "uk-12",
        "size": "UK 12",
        "price": {"current": 8500, "was": None, "currency": "GBP"},
        "inStock": False,
        "url": "/product/87654321",
    },
]

_SIGNIN_PAGE = """<!doctype html><title>Sign in</title>
<form method="post" action="/signin">
  <input name="email" value="shopper@example.co.uk">
  <button id="signin" type="submit">Sign in</button>
</form>
"""

_ACCOUNT_PAGE = """<!doctype html><title>Saved items</title>
<h1>Saved items</h1><div id="items">loading</div>
<script>
  localStorage.setItem('auth.bearer', %(bearer)s);
  localStorage.setItem('ui.theme', 'light');
  fetch('/api/saved-items', {headers: {'Authorization': 'Bearer ' + %(bearer)s}})
    .then(r => r.json())
    .then(d => { document.getElementById('items').textContent =
                 'loaded ' + d.savedItems.length; });
</script>
"""


@dataclass
class FakeSite:
    """A running fake site. Use as a context manager."""

    empty_list: bool = False
    reject_session: bool = False
    _server: ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("site is not running")
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> FakeSite:
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                """Silence the default stderr access log."""

            def _send(
                self,
                status: int,
                body: bytes,
                content_type: str,
                extra: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _authenticated(self) -> bool:
                if site.reject_session:
                    return False
                return f"{SESSION_COOKIE}={SESSION_TOKEN}" in (self.headers.get("Cookie") or "")

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    self._send(200, _SIGNIN_PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/account":
                    page = _ACCOUNT_PAGE % {"bearer": json.dumps(BEARER_TOKEN)}
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif path == "/api/saved-items":
                    self._saved_items()
                elif path == "/static/app.css":
                    # Noise: proves non-JSON resource types are filtered out.
                    self._send(200, b"body{color:#000}", "text/css")
                else:
                    self._send(404, b"not found", "text/plain")

            def _saved_items(self) -> None:
                if not self._authenticated():
                    body = json.dumps({"error": "not_authenticated"}).encode()
                    self._send(401, body, "application/json")
                    return
                payload = {
                    "customerId": CUSTOMER_ID,
                    "customerEmail": "shopper@example.co.uk",
                    "sessionToken": SESSION_TOKEN,
                    "savedItems": [] if site.empty_list else SAVED_ITEMS,
                }
                self._send(200, json.dumps(payload).encode(), "application/json")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self._send(
                    303,
                    b"",
                    "text/plain",
                    {
                        "Set-Cookie": (f"{SESSION_COOKIE}={SESSION_TOKEN}; Path=/; Max-Age=86400"),
                        "Location": "/account",
                    },
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
