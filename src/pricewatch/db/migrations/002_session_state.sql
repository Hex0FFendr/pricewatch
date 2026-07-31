-- Track when each account's session was last captured.
--
-- Stored on the account rather than derived from the session file's mtime: the
-- file gets rewritten by operations that do not re-authenticate, so its mtime
-- answers "when did we last touch this" rather than "when did you last sign in",
-- and only the latter is useful when a session goes bad.

ALTER TABLE accounts ADD COLUMN session_captured_at TEXT;
