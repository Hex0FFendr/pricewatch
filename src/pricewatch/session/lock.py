"""Exclusive locking for browser profile directories.

Chromium takes an exclusive lock on a profile directory: a second process
trying to use the same profile fails, sometimes messily. Since the daemon polls
on a schedule and `pricewatch login` is run by hand, the two will eventually
collide. This makes the collision a clear message instead of a corrupted
profile.

The lock is advisory `flock` on a sidecar file. The holder writes its pid and
what it is doing, so the loser can say who has it rather than just "busy".
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pricewatch.clock import utcnow_iso
from pricewatch.errors import PricewatchError


class ProfileBusyError(PricewatchError):
    """Another process is using this account's browser profile."""


@contextmanager
def profile_lock(lock_path: Path, *, owner: str, timeout: float = 0.0) -> Iterator[None]:
    """Hold an exclusive lock on a profile for the duration of the block.

    Args:
        lock_path: sidecar lock file; created if absent.
        owner: short description of what wants the profile, e.g. "login".
        timeout: seconds to keep retrying before giving up. 0 fails immediately.

    Raises:
        ProfileBusyError: the lock could not be acquired within `timeout`.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout

    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise ProfileBusyError(
                        f"browser profile is in use by {_describe_holder(lock_path)}. "
                        f"Wait for it to finish, or stop the pricewatch daemon first."
                    ) from exc
                time.sleep(0.1)

        os.ftruncate(handle, 0)
        os.write(handle, f"pid={os.getpid()} owner={owner} since={utcnow_iso()}\n".encode())
        os.fsync(handle)

        try:
            yield
        finally:
            os.ftruncate(handle, 0)
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _describe_holder(lock_path: Path) -> str:
    """Best-effort description of the current holder. flock is advisory, so the
    file stays readable while another process holds the lock."""
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "another process"
    return content or "another process"
