"""Exception hierarchy.

Kept in one module so that callers can catch `PricewatchError` at the process
boundary and everything below it is guaranteed to carry an operator-readable
message. Anything that escapes as a bare `Exception` is a bug.
"""

from __future__ import annotations


class PricewatchError(Exception):
    """Base class for every error this application raises deliberately."""


class ConfigError(PricewatchError):
    """Configuration file is missing, malformed, or semantically invalid."""


class MigrationError(PricewatchError):
    """The database schema could not be brought up to date."""


class SessionExpiredError(PricewatchError):
    """A stored session is no longer authenticated.

    Distinct from a transport failure on purpose: an expired session needs a
    human at a keyboard, whereas a transport failure just needs a retry. The
    daemon's backoff logic keys off this difference, and the adapter contract
    requires adapters to raise this and nothing else when identity is absent.
    """


class AdapterError(PricewatchError):
    """A site adapter failed in a way that is not a session expiry."""
