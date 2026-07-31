"""Site adapters.

Known sites and their starting pages live here. API paths do not: those come
from a `pricewatch discover` capture against a real signed-in session, and are
written into a concrete adapter in phases 6 and 7.
"""

from __future__ import annotations

from pricewatch.adapters.base import (
    Adapter,
    AdapterRegistry,
    ExecMode,
    SavedItem,
    SavedItemsResult,
    SiteProfile,
    registry,
)

#: Descriptive entries for the sites this tool targets. Having a profile here
#: does not mean an adapter exists — check `registry.available(site_id)`.
KNOWN_SITES: dict[str, SiteProfile] = {
    "asos": SiteProfile(
        site_id="asos",
        display_name="ASOS",
        notes=(
            "Fronted by a bot-management layer whose cookies are tied to the TLS "
            "fingerprint that earned them, and account identity is expected to involve a "
            "token in localStorage rather than cookies alone. Whether the HTTP fast path "
            "is usable is an open question that only a capture can settle."
        ),
    ),
    "freepeople": SiteProfile(
        site_id="freepeople",
        display_name="Free People",
        notes="Lighter protection expected than ASOS; session-cookie replay is plausible.",
    ),
}

__all__ = [
    "KNOWN_SITES",
    "Adapter",
    "AdapterRegistry",
    "ExecMode",
    "SavedItem",
    "SavedItemsResult",
    "SiteProfile",
    "registry",
]
