"""Credential and PII scrubbing.

This module is the single implementation of redaction in the codebase. Two
callers depend on it and they must not drift apart:

1. The structlog processor (`pricewatch.logging`), which scrubs every log event.
2. The `discover` capture writer (phase 2), which scrubs HTTP captures *at the
   moment of capture*, before anything touches disk.

Point 2 is the reason this is a standalone pure function rather than a logging
detail. A capture of a saved-items response contains bearer tokens, session
cookies, and the account holder's name and delivery address. Redacting on the
way out of the file would leave a window where the unredacted bytes exist on
disk; redacting on the way in means they never do.

Two independent strategies run, and both must be conservative in the same
direction — over-redact rather than under-redact:

* Key-based: any mapping key whose name suggests a credential has its entire
  value replaced, whatever the value's type.
* Value-based: free text is pattern-scrubbed, because credentials leak in
  places that are not helpfully named (exception messages, response bodies,
  URLs with tokens in the query string).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED: Final = "[REDACTED]"

#: Maximum container nesting to walk before giving up and redacting wholesale.
#: Guards against both pathological nesting and reference cycles.
MAX_DEPTH: Final = 12

#: Substrings that mark a mapping key as credential-bearing. Matched
#: case-insensitively against the key with separators removed, so "api_key",
#: "api-key", "apiKey" and "APIKEY" all collapse to the same probe.
_SENSITIVE_KEY_PARTS: Final = frozenset(
    {
        "auth",
        "apikey",
        "bearer",
        "cookie",
        "credential",
        "csrf",
        "jsessionid",
        "jwt",
        "nonce",
        "password",
        "passwd",
        "privatekey",
        "pwd",
        "refreshtoken",
        "secret",
        "session",
        "sessionid",
        "setcookie",
        "signature",
        "token",
        "xsrf",
    }
)

#: Keys that contain a sensitive substring but name *metadata about* a
#: credential rather than the credential: its name, count, timestamp, expiry,
#: status or source. Checked before `_SENSITIVE_KEY_PARTS`, on the same
#: normalised form.
#:
#: Allowlisting is not a blanket exemption. It only stops the key-based rule
#: from blanking the value wholesale; the value is still walked and every string
#: inside it still goes through `redact_text`. So a credential that turned up
#: under an allowlisted key would still be caught by the value-based rules.
#:
#: Without these, `StoredSession.summary()` — which exists specifically to be
#: logged — comes out as a row of `[REDACTED]`, and the log line that is
#: supposed to make a broken session diagnosable says nothing at all.
_KEY_ALLOWLIST: Final = frozenset(
    {
        "authenticated",
        "cookiecount",
        "cookienames",
        "cookiepolicy",
        "hastoken",
        "localstoragekeys",
        "sessioncapturedat",
        "sessionexpiresat",
        "sessionsource",
        "sessionstate",
        "sessionstatus",
        "tokenenv",
        "tokenexpiresat",
        "tokennames",
    }
)

_KEY_SEPARATORS: Final = re.compile(r"[-_\s.]+")


def _normalise_key(key: str) -> str:
    return _KEY_SEPARATORS.sub("", key).lower()


def is_sensitive_key(key: str) -> bool:
    """Whether a mapping key's *value* should be redacted on name alone."""
    probe = _normalise_key(key)
    if probe in _KEY_ALLOWLIST:
        return False
    return any(part in probe for part in _SENSITIVE_KEY_PARTS)


# Ordered most-specific first: an Authorization header value should be caught by
# the bearer rule and reported as such, not shredded by the generic blob rule.
_TEXT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # JSON Web Tokens, including the unsigned two-segment form.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?")),
    # "Authorization: Bearer <token>" and friends.
    ("bearer", re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")),
    # Cookie pairs: name=value where the value is long enough to be a secret.
    # Deliberately also catches tokens in URL query strings.
    ("kv", re.compile(r"\b([A-Za-z0-9_-]{2,})=([A-Za-z0-9%._~+/-]{16,}=*)")),
    # Standalone high-entropy blobs: hex digests, base64 payloads.
    ("hex", re.compile(r"\b[A-Fa-f0-9]{32,}\b")),
    ("b64", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
    # PII that shows up in UK retail account payloads.
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "postcode",
        re.compile(r"(?i)\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),
    ),
)


def _replace_kv(match: re.Match[str]) -> str:
    """Keep the key of a `key=value` pair, redact the value."""
    return f"{match.group(1)}={REDACTED}"


def redact_text(text: str) -> str:
    """Scrub credential- and PII-shaped substrings from free text."""
    for name, pattern in _TEXT_PATTERNS:
        text = pattern.sub(_replace_kv if name == "kv" else REDACTED, text)
    return text


def redact(value: object, *, _depth: int = 0) -> Any:  # noqa: ANN401
    """Return `value` with credentials and PII removed.

    Containers are rebuilt rather than mutated, so the caller's object is never
    modified. Types this function does not understand are passed through
    untouched *only* if they are immutable scalars; anything else is rendered
    via `repr` and text-scrubbed, on the principle that an unknown object may
    have a credential in its representation.
    """
    if _depth > MAX_DEPTH:
        return REDACTED

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, bytes | bytearray):
        # Never try to interpret binary payloads; length alone is the useful part.
        return f"[REDACTED {len(value)} bytes]"

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out

    if isinstance(value, set | frozenset):
        return {redact(item, _depth=_depth + 1) for item in value}

    if isinstance(value, Sequence):
        rebuilt = [redact(item, _depth=_depth + 1) for item in value]
        return tuple(rebuilt) if isinstance(value, tuple) else rebuilt

    return redact_text(repr(value))
