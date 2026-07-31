"""Redaction tests.

These are the tests that stop credentials reaching disk, so they lean towards
asserting on *absence of the secret* rather than on exact output formatting —
a formatting change should not be able to make a leak test pass.
"""

from __future__ import annotations

import json

import pytest
import structlog

from pricewatch.logging import configure_logging, redaction_processor
from pricewatch.redaction import REDACTED, is_sensitive_key, redact, redact_text

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
ABCK = "7A2E4F1B" * 12  # Akamai-style opaque blob


@pytest.mark.parametrize(
    "key",
    [
        "cookie",
        "Cookie",
        "set-cookie",
        "Set-Cookie",
        "authorization",
        "Authorization",
        "api_key",
        "apiKey",
        "API-KEY",
        "access_token",
        "refresh_token",
        "bearerToken",
        "password",
        "passwd",
        "sessionId",
        "JSESSIONID",
        "x-csrf-token",
        "client_secret",
        "signature",
    ],
)
def test_sensitive_keys_are_recognised(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "price",
        "title",
        "url",
        "item_id",
        "currency",
        "in_stock",
        # Allowlisted: these name a fact *about* a credential, not the credential.
        "session_status",
        "session_expires_at",
        "token_env",
        "has_token",
        "authenticated",
        "cookie_count",
    ],
)
def test_ordinary_keys_are_preserved(key: str) -> None:
    assert not is_sensitive_key(key)


def test_sensitive_key_redacts_whole_value_regardless_of_type() -> None:
    payload = {
        "cookies": [{"name": "_abck", "value": ABCK}],
        "authorization": {"scheme": "Bearer", "token": JWT},
    }
    result = redact(payload)
    assert result == {"cookies": REDACTED, "authorization": REDACTED}
    assert ABCK not in json.dumps(result)
    assert JWT not in json.dumps(result)


def test_nested_structures_are_walked() -> None:
    payload = {
        "account": "asos-uk",
        "response": {
            "items": [
                {"id": 12345678, "price": 2999, "token": JWT},
                {"id": 87654321, "price": 4500},
            ]
        },
    }
    result = redact(payload)
    assert result["account"] == "asos-uk"
    assert result["response"]["items"][0]["id"] == 12345678
    assert result["response"]["items"][0]["price"] == 2999
    assert result["response"]["items"][0]["token"] == REDACTED
    assert result["response"]["items"][1] == {"id": 87654321, "price": 4500}


def test_input_is_not_mutated() -> None:
    payload = {"token": JWT, "nested": {"cookie": "a=b"}}
    redact(payload)
    assert payload["token"] == JWT
    assert payload["nested"] == {"cookie": "a=b"}


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {JWT}",
        f"token={JWT}",
        f"failed to parse response, got {JWT} instead",
        f"Cookie: _abck={ABCK}; sessionId=9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        "https://api.example.co.uk/saved?access_token=A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
        "customer email is shopper.name@gmail.com",
    ],
)
def test_credential_shaped_text_is_scrubbed(text: str) -> None:
    scrubbed = redact_text(text)
    assert JWT not in scrubbed
    assert ABCK not in scrubbed
    assert "9f8e7d6c5b4a39281706f5e4d3c2b1a0" not in scrubbed
    assert "A1b2C3d4E5f6G7h8I9j0K1l2M3n4" not in scrubbed
    assert "shopper.name@gmail.com" not in scrubbed


def test_key_value_scrubbing_keeps_the_key() -> None:
    # Knowing *which* cookie was present is useful for debugging; its value is not.
    assert redact_text(f"_abck={ABCK}").startswith("_abck=")


def test_uk_postcode_is_scrubbed() -> None:
    # Saved-items and basket payloads carry the delivery address.
    for postcode in ("SW1A 1AA", "EC1A1BB", "m1 1ae"):
        assert postcode not in redact_text(f"deliver to {postcode}")


def test_product_urls_and_prices_survive() -> None:
    # Over-redaction that eats the data we exist to collect is also a failure.
    text = "https://www.asos.com/some-brand/some-dress/prd/12345678 now GBP 29.99 was 59.99"
    assert redact_text(text) == text


def test_bytes_are_never_interpreted() -> None:
    blob = b"\x00\x01binary-session-blob"
    assert redact(blob) == f"[REDACTED {len(blob)} bytes]"


def test_scalars_pass_through() -> None:
    assert redact(None) is None
    assert redact(True) is True
    assert redact(2999) == 2999
    assert redact(29.99) == 29.99


def test_sequence_types_are_preserved() -> None:
    assert redact(["a", "b"]) == ["a", "b"]
    assert redact(("a", "b")) == ("a", "b")
    assert redact({"a", "b"}) == {"a", "b"}


def test_depth_limit_stops_runaway_nesting() -> None:
    payload: dict[str, object] = {"leaf": JWT}
    for _ in range(40):
        payload = {"nested": payload}
    assert JWT not in json.dumps(redact(payload))


def test_unknown_objects_are_reprd_and_scrubbed() -> None:
    class Session:
        def __repr__(self) -> str:
            return f"<Session token={JWT}>"

    assert JWT not in str(redact(Session()))


class TestLoggingIntegration:
    """The processor must scrub through the real structlog chain, not just alone."""

    def test_processor_scrubs_event_dict(self) -> None:
        result = redaction_processor(None, "info", {"event": "polled", "cookie": ABCK})
        assert result["event"] == "polled"
        assert result["cookie"] == REDACTED

    def test_secrets_do_not_reach_the_rendered_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging("info", force_json=True)
        structlog.get_logger("test").info(
            "session refreshed", account="asos-uk", set_cookie=f"_abck={ABCK}", note=JWT
        )
        captured = capsys.readouterr().err
        assert "asos-uk" in captured
        assert ABCK not in captured
        assert JWT not in captured

    def test_bound_context_is_scrubbed_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A secret bound far from the call site must still be caught.
        configure_logging("info", force_json=True)
        structlog.get_logger("test").bind(auth_token=JWT).info("fetching saved items")
        assert JWT not in capsys.readouterr().err

    def test_traceback_text_is_scrubbed(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging("info", force_json=True)
        try:
            raise RuntimeError(f"refused with Bearer {JWT}")
        except RuntimeError:
            structlog.get_logger("test").exception("adapter failed")
        assert JWT not in capsys.readouterr().err
