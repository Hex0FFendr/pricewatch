"""The site adapter contract.

Everything site-specific lives behind this interface. The rest of the
application never learns that ASOS and Free People are different.

The single most important rule here is encoded in `SavedItemsResult`: a result
cannot be constructed without a non-empty `identity`. That closes a hole in the
original design, which inferred session death from an empty item list. Emptying
your saved list is a normal thing to do, and a rule that reads "empty means the
session died" would pin the account to `needs_reauth` forever, with re-login
unable to fix it because the list is still legitimately empty.

So liveness is never inferred from the payload's contents. An adapter must
extract the site's own identifier for the signed-in account — a customer id, a
profile handle, whatever the response carries — and if it cannot, it raises
`SessionExpiredError`. Authenticated-and-empty is then a fact we can record;
no-identity is a dead session. The type system enforces the distinction rather
than trusting each adapter to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pricewatch.errors import AdapterError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from pricewatch.session.store import StoredSession


class ExecMode(StrEnum):
    """How a fetch was carried out.

    `BROWSER` drives a real Chromium against the site and is the path every
    adapter must implement. `HTTP` replays the exported session directly and is
    optional — whether it works at all is a per-site runtime property, so the
    engine tries it, demotes to `BROWSER` on rejection, and records which one
    actually succeeded.
    """

    BROWSER = "browser"
    HTTP = "http"


@dataclass(frozen=True, slots=True)
class SavedItem:
    """One entry in a saved-items list, normalised across sites."""

    #: The site's own product identifier.
    external_id: str
    title: str
    #: Minor units (pence). Never a float.
    price: int
    currency: str

    #: Saved size/colour. Empty string — never None — when the item was saved
    #: without choosing one, matching the database's NOT NULL DEFAULT ''.
    variant_id: str = ""
    variant_label: str | None = None

    #: Site-reported "was" price, if stated. Not trusted as a stable baseline;
    #: the percent-off trigger also considers the highest price we have seen.
    rrp: int | None = None

    #: Tri-state on purpose. `None` means the response did not say, which is
    #: different from "out of stock" and must not let back-in-stock fire on a
    #: parse gap.
    in_stock: bool | None = None

    url: str | None = None
    image_url: str | None = None

    def __post_init__(self) -> None:
        if not self.external_id:
            raise AdapterError("saved item has no external_id")
        if self.price < 0:
            raise AdapterError(f"saved item {self.external_id} has a negative price")
        if len(self.currency) != 3:
            raise AdapterError(
                f"saved item {self.external_id} has a non-ISO currency {self.currency!r}"
            )


@dataclass(frozen=True, slots=True)
class SavedItemsResult:
    """A successful, authenticated read of a saved-items list."""

    #: The site's identifier for the signed-in account. Proof of authentication,
    #: and the reason an empty `items` tuple is unambiguous.
    identity: str
    items: tuple[SavedItem, ...]
    exec_mode: ExecMode
    #: Redacted response body, retained for debugging a bad parse after the fact.
    raw: str = ""
    #: Number of extra per-item lookups this fetch cost, for the case where the
    #: list response carried no per-variant stock.
    extra_lookups: int = 0

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise AdapterError(
                "adapter returned a result without an account identity; an adapter that "
                "cannot establish who it is signed in as must raise SessionExpiredError"
            )

    @property
    def is_empty(self) -> bool:
        """True for an authenticated read of a genuinely empty saved list."""
        return not self.items


@dataclass(frozen=True, slots=True)
class SiteProfile:
    """Static, non-secret facts about a site.

    Deliberately holds no API paths. Those are discovered per account with
    `pricewatch discover` and written into the adapter from a real capture,
    never guessed.
    """

    site_id: str
    display_name: str
    #: Starting page for `login` and `discover`. Optional: if it is not set here
    #: or in the account config, the browser opens blank and you navigate.
    home_url: str | None = None
    notes: str = ""


@runtime_checkable
class Adapter(Protocol):
    """What a site adapter must provide."""

    #: Not a ClassVar: an adapter may legitimately build its profile per
    #: instance, and requiring a class-level attribute would exclude that.
    site: SiteProfile
    #: Whether this adapter implements `fetch_via_http`. Declaring True is a
    #: claim that the fast path exists, not that it will succeed on any given
    #: cycle; rejection is expected and handled by demoting to the browser path.
    supports_http_mode: bool

    def probe_identity(self, context: BrowserContext) -> str | None:
        """Return the signed-in account's site identifier, or None if signed out.

        Used by `login` to tell "the user finished signing in" from "the page
        merely finished loading". Must not raise for the signed-out case.
        """
        ...

    def fetch_via_browser(self, context: BrowserContext) -> SavedItemsResult:
        """Read the saved-items list by driving a real browser.

        Every adapter implements this; it is the guaranteed path.

        Raises:
            SessionExpiredError: the session is no longer authenticated.
            AdapterError: anything else, including an unparseable response.
        """
        ...

    def fetch_via_http(self, session: StoredSession) -> SavedItemsResult:
        """Read the saved-items list by replaying the exported session.

        Only called when `supports_http_mode` is True. Raising
        `SessionExpiredError` here is routine — it means the fast path was
        rejected — and causes a demotion to `fetch_via_browser` for that cycle.
        """
        ...


@dataclass(slots=True)
class AdapterRegistry:
    """Maps site ids to adapter instances."""

    _adapters: dict[str, Adapter] = field(default_factory=dict)

    def register(self, adapter: Adapter) -> None:
        site_id = adapter.site.site_id
        if site_id in self._adapters:
            raise AdapterError(f"an adapter for {site_id!r} is already registered")
        self._adapters[site_id] = adapter

    def get(self, site_id: str) -> Adapter:
        try:
            return self._adapters[site_id]
        except KeyError:
            raise AdapterError(
                f"no adapter is implemented for {site_id!r} yet. Adapters are written from a "
                f"real capture, not from guesswork: run `pricewatch discover <account>` while "
                f"signed in, then the adapter follows from the recorded traffic."
            ) from None

    def unregister(self, site_id: str) -> None:
        """Remove an adapter. Exists so tests can install a fake and clean up."""
        self._adapters.pop(site_id, None)

    def available(self, site_id: str) -> bool:
        return site_id in self._adapters

    def site_ids(self) -> list[str]:
        return sorted(self._adapters)


#: Process-wide registry. Real site adapters land here in phases 6 and 7.
registry = AdapterRegistry()
