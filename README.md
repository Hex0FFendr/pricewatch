# pricewatch

Self-hosted price and stock monitor for your own saved-items lists on UK
retail accounts (ASOS, Free People). It signs in as you, once, then polls your
saved list on a schedule and notifies you when something you already wanted
gets cheaper or comes back in your size.

Design constraints it is built around:

- **One request per site per cycle.** It reads the saved-items list, not
  individual product pages. Polling floor is 30 minutes, default 3 hours,
  jittered.
- **GBP / `.co.uk` only.** No multi-region handling.
- **Your account, your machine.** Sessions never leave the host.

## Status

Built in phases. Phase 1 is complete and the gates below are green.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Config, database + migrations, CLI skeleton, logging + redaction | **done** |
| 2 | Session store, expiry detection, `login`, `discover` | not started |
| 3 | Reconciliation, trigger engine, `target`, `history` | not started |
| 4 | Notifiers (ntfy, Telegram, Discord, SMTP), batching | not started |
| 5 | Daemon loop, backoff, systemd units | not started |
| 6 | ASOS adapter | blocked — needs a `discover` capture |
| 7 | Free People adapter | blocked — needs a `discover` capture |

Phases 6 and 7 cannot be written from documentation or guesswork; they need a
recorded capture of the real saved-items traffic from a signed-in session. Run
`pricewatch discover <account>` once phase 2 lands and the adapter follows from
the (redacted) output.

Commands that are not built yet are still registered and exit with code `3` and
a message naming the phase, so a script can tell "not built" from "failed".

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pricewatch --help
```

## Quick start

```sh
mkdir -p ~/.config/pricewatch
cp pricewatch.example.toml ~/.config/pricewatch/config.toml
$EDITOR ~/.config/pricewatch/config.toml

uv run pricewatch config check   # validates, and resolves every secret now
uv run pricewatch init           # creates the database, applies migrations
uv run pricewatch accounts       # shows session health per account
```

`config check` resolves notifier secrets at check time rather than at send
time — an unset token should surface while you are looking at the terminal, not
when a price finally drops.

## Configuration

TOML, at `$XDG_CONFIG_HOME/pricewatch/config.toml` by default. Override with
`--config` or `PRICEWATCH_CONFIG`. See `pricewatch.example.toml` for the
annotated full set.

Unknown keys are rejected rather than ignored. Secrets take either a literal
string or an environment reference:

```toml
token = { env = "PRICEWATCH_NTFY_TOKEN" }
```

## Triggers

| Trigger | Fires when |
|---------|-----------|
| `any_drop` | Price is below the previous observation |
| `percent_off` | Price is at least N% below the reference price |
| `target_price` | Price is at or below a per-item threshold you set |
| `lowest_ever` | Price is at or below every price previously recorded |
| `back_in_stock` | Your saved size goes out-of-stock → in-stock |

Two details worth knowing:

**The `percent_off` reference is not just the site's RRP.** Retailers rewrite
the "was" price, and a reset RRP would silently disarm the trigger. pricewatch
uses `max(site-reported RRP, highest price it has observed itself)`.

**Repeat alerts are bounded by price *streak*, not by time.** The dedupe key is
`(item, trigger, price, streak_seq)`. A price that sits at £30 for a week
alerts once. A price that drops to £30, rises, and drops to £30 again alerts
twice, because the rise starts a new streak. This is enforced by a database
constraint, not by application logic.

## Data model notes

- Money is stored as integer pence. No float ever touches a price comparison.
- `items.variant_id` is `NOT NULL DEFAULT ''` rather than nullable, because
  SQLite treats NULLs as distinct in a UNIQUE index — a nullable column would
  permit duplicate rows for an item saved without a size.
- `observations.currency` is recorded per observation even though the tool is
  GBP-only, so that a currency change is detectable and can never be read as a
  price change.
- `observations.stock_known` separates "out of stock" from "the response did not
  say", so `back_in_stock` cannot fire on a parse gap.
- Items removed from your saved list are marked `removed_at`, not deleted.
  Re-saving an item keeps its price history.

## Redaction

`pricewatch.redaction` is the single implementation, shared by the log
processor and (in phase 2) the `discover` capture writer. Captures are scrubbed
*at the moment of capture*, before anything reaches disk — redacting on the way
out would leave a window where unredacted bytes exist on disk.

It redacts on two independent axes: mapping keys whose names suggest a
credential, and free text matching credential or PII shapes (JWTs, bearer
headers, long `key=value` pairs, hex/base64 blobs, email addresses, UK
postcodes). Product URLs and prices are deliberately preserved — over-redaction
that eats the data the tool exists to collect is also a failure, and there is a
test for it.

`profiles/`, `sessions/`, `scratch/`, `captures/` and `config.toml` are
gitignored from the first commit.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Migrations are numbered `.sql` files under `src/pricewatch/db/migrations/`,
applied in order inside a transaction together with their own bookkeeping row.
Applied migrations are checksummed: editing one that has already run is
detected and refused. Add a new migration instead.

## Legal

Both sites' terms prohibit automated access. This polls your own account, your
own saved list, a handful of times a day. The realistic exposure is
account-level action by the retailer rather than anything legal — worth knowing
before you run it.
