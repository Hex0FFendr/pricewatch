from __future__ import annotations

import pytest

from pricewatch.adapters import KNOWN_SITES, registry
from pricewatch.adapters.base import (
    Adapter,
    AdapterRegistry,
    ExecMode,
    SavedItem,
    SavedItemsResult,
    SiteProfile,
)
from pricewatch.errors import AdapterError


class TestSavedItem:
    def test_valid_item(self) -> None:
        item = SavedItem(external_id="1", title="Dress", price=2999, currency="GBP")
        assert item.variant_id == ""  # matches the DB's NOT NULL DEFAULT ''
        assert item.in_stock is None  # tri-state: "not stated"

    def test_missing_external_id_is_refused(self) -> None:
        with pytest.raises(AdapterError, match="no external_id"):
            SavedItem(external_id="", title="Dress", price=2999, currency="GBP")

    def test_negative_price_is_refused(self) -> None:
        with pytest.raises(AdapterError, match="negative price"):
            SavedItem(external_id="1", title="Dress", price=-1, currency="GBP")

    def test_non_iso_currency_is_refused(self) -> None:
        with pytest.raises(AdapterError, match="non-ISO currency"):
            SavedItem(external_id="1", title="Dress", price=2999, currency="pounds")

    def test_items_are_immutable(self) -> None:
        item = SavedItem(external_id="1", title="Dress", price=2999, currency="GBP")
        with pytest.raises(AttributeError):
            item.price = 100  # type: ignore[misc]


class TestSavedItemsResult:
    def test_identity_is_mandatory(self) -> None:
        # The rule that stops an empty saved list being read as a dead session.
        with pytest.raises(AdapterError, match="SessionExpiredError"):
            SavedItemsResult(identity="", items=(), exec_mode=ExecMode.BROWSER)

    def test_whitespace_identity_does_not_count(self) -> None:
        with pytest.raises(AdapterError):
            SavedItemsResult(identity="   ", items=(), exec_mode=ExecMode.BROWSER)

    def test_authenticated_empty_list_is_representable(self) -> None:
        result = SavedItemsResult(identity="cust-1", items=(), exec_mode=ExecMode.BROWSER)
        assert result.is_empty
        assert result.identity == "cust-1"

    def test_non_empty_result(self) -> None:
        item = SavedItem(external_id="1", title="Dress", price=2999, currency="GBP")
        result = SavedItemsResult(identity="cust-1", items=(item,), exec_mode=ExecMode.HTTP)
        assert not result.is_empty
        assert result.exec_mode == ExecMode.HTTP


class _StubAdapter:
    site = SiteProfile(site_id="stub", display_name="Stub")
    supports_http_mode = False

    def probe_identity(self, context: object) -> str | None:
        return None

    def fetch_via_browser(self, context: object) -> SavedItemsResult:
        return SavedItemsResult(identity="x", items=(), exec_mode=ExecMode.BROWSER)

    def fetch_via_http(self, session: object) -> SavedItemsResult:
        raise AdapterError("no http mode")


class TestRegistry:
    def test_register_and_get(self) -> None:
        reg = AdapterRegistry()
        adapter = _StubAdapter()
        reg.register(adapter)
        assert reg.get("stub") is adapter
        assert reg.available("stub")
        assert reg.site_ids() == ["stub"]

    def test_double_registration_is_refused(self) -> None:
        reg = AdapterRegistry()
        reg.register(_StubAdapter())
        with pytest.raises(AdapterError, match="already registered"):
            reg.register(_StubAdapter())

    def test_missing_adapter_points_at_discover(self) -> None:
        # The error has to say what to do, because "no adapter" is the expected
        # state for a site nobody has captured yet.
        reg = AdapterRegistry()
        with pytest.raises(AdapterError, match="pricewatch discover"):
            reg.get("asos")

    def test_unregister(self) -> None:
        reg = AdapterRegistry()
        reg.register(_StubAdapter())
        reg.unregister("stub")
        assert not reg.available("stub")

    def test_stub_satisfies_the_protocol(self) -> None:
        assert isinstance(_StubAdapter(), Adapter)


class TestKnownSites:
    def test_target_sites_are_described(self) -> None:
        assert set(KNOWN_SITES) == {"asos", "freepeople"}

    def test_no_api_paths_are_shipped(self) -> None:
        # The whole point: nothing here encodes a guess about a real endpoint.
        for profile in KNOWN_SITES.values():
            assert profile.home_url is None

    def test_no_real_adapters_are_registered_yet(self) -> None:
        assert not registry.available("asos")
        assert not registry.available("freepeople")
