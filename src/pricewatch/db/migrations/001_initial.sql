-- Initial schema.
--
-- Conventions:
--   * All timestamps are UTC ISO-8601 text, written by pricewatch.clock.
--   * All money is an INTEGER count of minor units (pence). No floats anywhere
--     near a price comparison.
--   * Booleans are INTEGER 0/1 with a CHECK constraint.

CREATE TABLE accounts (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT    NOT NULL UNIQUE,
    site                 TEXT    NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),

    -- Lifecycle state, driven by the poll loop:
    --   unknown       never polled
    --   ok            last cycle authenticated and parsed
    --   needs_reauth  session is dead; requires `pricewatch login`
    --   degraded      repeated transport/parse failures, still retrying
    status               TEXT    NOT NULL DEFAULT 'unknown'
                                 CHECK (status IN ('unknown', 'ok', 'needs_reauth', 'degraded')),
    last_ok_at           TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,

    -- Which execution path last succeeded. Surfaced by `pricewatch accounts`
    -- so that the browser-vs-http question is answered by observation.
    last_exec_mode       TEXT    CHECK (last_exec_mode IN ('browser', 'http')),

    created_at           TEXT    NOT NULL
);

CREATE TABLE poll_runs (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    exec_mode   TEXT    CHECK (exec_mode IN ('browser', 'http')),
    status      TEXT    NOT NULL
                        CHECK (status IN ('running', 'ok', 'failed', 'needs_reauth')),
    item_count  INTEGER,
    error       TEXT
);

CREATE INDEX poll_runs_account_started ON poll_runs (account_id, started_at DESC);

CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,

    -- Site's own product identifier.
    external_id   TEXT    NOT NULL,
    -- Saved size/colour variant. Empty string, never NULL, for "saved without a
    -- size": SQLite treats NULLs as distinct in a UNIQUE index, so a nullable
    -- column here would silently permit duplicate rows for the same item.
    variant_id    TEXT    NOT NULL DEFAULT '',
    variant_label TEXT,

    title         TEXT    NOT NULL,
    url           TEXT,
    image_url     TEXT,

    -- Retained per item even though the tool is GBP-only today: it makes a
    -- currency change detectable, and a currency change must never be read as
    -- a price change.
    currency      TEXT    NOT NULL DEFAULT 'GBP',

    -- User-set alert threshold in minor units; NULL means no target.
    target_price  INTEGER CHECK (target_price IS NULL OR target_price > 0),

    -- Highest price we have observed ourselves. Used alongside the site's
    -- reported RRP because retailers reset the "was" price, which would
    -- otherwise silently disarm the percent-off trigger.
    highest_seen_price INTEGER,

    -- Incremented whenever an observation is higher than its predecessor. This
    -- is what makes "price dropped to £30, rose, dropped to £30 again" two
    -- notifiable events while a flat £30 stays one. See notifications.
    price_streak_seq   INTEGER NOT NULL DEFAULT 0,

    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    -- Set when the item leaves the saved list; cleared if it is re-added, so
    -- that re-saving an item keeps its price history rather than starting over.
    removed_at    TEXT,

    UNIQUE (account_id, external_id, variant_id)
);

CREATE INDEX items_account_active ON items (account_id) WHERE removed_at IS NULL;

CREATE TABLE observations (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    poll_run_id INTEGER REFERENCES poll_runs (id) ON DELETE SET NULL,
    observed_at TEXT    NOT NULL,

    price       INTEGER NOT NULL CHECK (price >= 0),
    rrp         INTEGER CHECK (rrp IS NULL OR rrp >= 0),
    currency    TEXT    NOT NULL,

    in_stock    INTEGER NOT NULL CHECK (in_stock IN (0, 1)),
    -- 0 when the saved-items response carried no per-variant stock information.
    -- Distinguishing "out of stock" from "not stated" keeps back-in-stock from
    -- firing on a parse gap.
    stock_known INTEGER NOT NULL DEFAULT 1 CHECK (stock_known IN (0, 1))
);

CREATE INDEX observations_item_time ON observations (item_id, observed_at DESC);

CREATE TABLE notifications (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    trigger_kind TEXT    NOT NULL
                         CHECK (trigger_kind IN ('any_drop', 'percent_off', 'target_price',
                                                 'lowest_ever', 'back_in_stock')),
    price        INTEGER NOT NULL,
    streak_seq   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,

    -- The dedupe key. One alert per (item, trigger, price level, streak). A
    -- price that sits at £30 for a week cannot re-notify; a price that returns
    -- to £30 after rising is a new streak and can.
    UNIQUE (item_id, trigger_kind, price, streak_seq)
);

CREATE INDEX notifications_item_created ON notifications (item_id, created_at DESC);

CREATE TABLE notification_deliveries (
    id              INTEGER PRIMARY KEY,
    notification_id INTEGER NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,
    notifier_name   TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      TEXT    NOT NULL,

    -- Delivery is tracked per notifier so that one dead webhook cannot suppress
    -- or duplicate delivery through the others.
    UNIQUE (notification_id, notifier_name)
);

CREATE TABLE raw_responses (
    id          INTEGER PRIMARY KEY,
    poll_run_id INTEGER NOT NULL REFERENCES poll_runs (id) ON DELETE CASCADE,
    captured_at TEXT    NOT NULL,
    -- Redacted by pricewatch.redaction before insert, never after.
    body        TEXT    NOT NULL
);

CREATE INDEX raw_responses_run ON raw_responses (poll_run_id);
