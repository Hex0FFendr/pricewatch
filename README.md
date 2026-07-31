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

Built in phases. Phases 1 and 2 are complete and the gates below are green.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Config, database + migrations, CLI skeleton, logging + redaction | **done** |
| 2 | Adapter contract, session store, `login`, `discover` | **done** |
| 3 | Reconciliation, trigger engine, `target`, `history` | not started |
| 4 | Notifiers (ntfy, Telegram, Discord, SMTP), batching | not started |
| 5 | Daemon loop, backoff, systemd units | not started |
| 6 | ASOS adapter | **needs a capture from you** |
| 7 | Free People adapter | **needs a capture from you** |

Phases 6 and 7 cannot be written from documentation or memory; they need a
recording of the real saved-items traffic from a signed-in session. That is what
`pricewatch discover` produces, and it is now built — see
[Unblocking the adapters](#unblocking-the-adapters).

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

uv run playwright install chromium   # needed from here on
uv run pricewatch login asos-uk      # opens a browser; sign in
uv run pricewatch discover asos-uk   # records the traffic an adapter needs
```

A headed browser needs a display. On a headless host, run under `xvfb-run -a`,
or point `PRICEWATCH_CHROMIUM_PATH` at an existing Chromium or Chrome binary if
Playwright's own download is not what you want to use.

## Unblocking the adapters

This is the step only you can do — it needs your account and a real sign-in.

```sh
uv run pricewatch login asos-uk --url https://www.asos.com
uv run pricewatch discover asos-uk
```

`discover` opens a browser on your existing profile. Navigate to your saved
items, then confirm at the prompt. Everything the page fetched is recorded and
ranked, largest JSON payload first, which is almost always the saved-items call:

```
Likeliest saved-items endpoints first:
  METHOD  STATUS        BYTES  JSON  ENDPOINT
  GET     200            8241  yes   api.example.co.uk/saved/lists
  GET     200             436  no    www.example.co.uk/my-account/saved
```

The capture is written to `<state_dir>/captures/`, mode 0600. Review it, then
hand it over and the adapter follows from it.

The capture also answers the open question about the HTTP fast path. If the
saved-items request carries only cookies, replaying it outside a browser is
plausible. If it carries an `Authorization` header, the fast path needs a token
with its own refresh flow, and driving the browser every cycle is likely the
better trade. `discover` records that a credential header was present while
redacting its value, so the question is answerable without exposing anything.

`pricewatch login` will tell you it cannot verify sign-in until an adapter
exists for that site — it saves the session as *unverified* rather than
implying a check it did not make.

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

## Sessions

Two artefacts per account, under `<state_dir>`:

- `profiles/<account>/` — a Chromium persistent profile. **The source of
  truth.** Keeping the whole profile rather than just cookies preserves the
  browser-side state a site ties a session to, which is what makes sessions
  survive.
- `sessions/<account>.json.enc` — an encrypted export of Playwright's
  `storage_state` (cookies + localStorage), *derived* from the profile. Only an
  HTTP fast path would read it; nothing loads it back into a browser.

These are alternatives, not partners: `launch_persistent_context` takes no
`storage_state` argument.

Encryption covers the export only, with a key from `PRICEWATCH_SESSION_KEY` or
an auto-generated 0600 key file. **This is partial by design** — the profile
directory beside it holds the same cookies in Chromium's own format and cannot
be encrypted without breaking Chromium. It protects against leakage through a
backup or a synced directory, not against someone who can read the state
directory as your user.

Chromium locks a profile directory exclusively, so the daemon and a manual
`login` would eventually collide. An advisory lockfile turns that into
`browser profile is in use by pid=… owner=daemon-poll` instead of a corrupted
profile.

```sh
uv run pricewatch session status asos-uk   # cookie names, expiries — never values
uv run pricewatch session clear asos-uk    # forget the session and the profile
```

### How session death is detected

Not by an empty saved-items list. Emptying your saved list is a normal thing to
do, and treating empty as "session died" would pin the account to
`needs_reauth` permanently, with re-login unable to help because the list is
still legitimately empty.

Instead, an adapter must extract the site's own identifier for the signed-in
account, and raise `SessionExpiredError` if it cannot. `SavedItemsResult`
refuses to be constructed without one, so authenticated-and-empty is a
representable fact and no-identity is unambiguous. The rule is enforced by the
type rather than left to each adapter to remember.

Cookie expiry is used only as a cheap hint to skip a pointless browser launch.
A site can invalidate a session long before its cookies expire and can keep one
alive past them, so it never decides anything on its own.

## Redaction

`pricewatch.redaction` is the single implementation, shared by the log
processor and the `discover` capture writer. Captures are scrubbed *at the
moment of capture*, before an exchange is even appended to the in-memory list —
scrubbing on the way out to the file would leave a window where unredacted
credentials existed on disk.

It redacts on two independent axes: mapping keys whose names suggest a
credential, and free text matching credential or PII shapes (JWTs, bearer
headers, long `key=value` pairs, hex/base64 blobs, email addresses, UK
postcodes).

**JSON bodies are redacted structurally, not text-scrubbed.** The body is
parsed, walked, and re-serialised, so field names, nesting and shapes survive
intact while credential-shaped values do not. That distinction is what makes a
capture usable: an adapter is written from the shape of the payload.

Over-redaction is treated as a failure too, not a safe default. Product URLs,
prices and titles are preserved, and there are tests for it — including one
that runs `StoredSession.summary()` through the redactor, because a "safe to
log" summary that comes out as a row of `[REDACTED]` makes a broken session
undiagnosable. A small allowlist covers keys that name *metadata about* a
credential (`cookie_names`, `session_expires_at`) rather than a credential.
Allowlisting only disables the key-based rule; values are still walked and
text-scrubbed, so it cannot become a leak.

`profiles/`, `sessions/`, `scratch/`, `captures/` and `config.toml` are
gitignored from the first commit.

## Development

```sh
uv run pytest
uv run pytest -m "not browser"   # skip the slow ones
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Tests marked `browser` drive a real Chromium against a real local HTTP server
(`tests/fake_site.py` — a generic cookie-gated saved-items API, deliberately not
an impersonation of either target site). Mocking Playwright would only prove the
mocks agree with each other; these prove that cookies survive a profile, that
`storage_state` exports what we think it does, and that the capture recorder
redacts real traffic. They start Xvfb themselves when there is no display.

Migrations are numbered `.sql` files under `src/pricewatch/db/migrations/`,
applied in order inside a transaction together with their own bookkeeping row.
Applied migrations are checksummed: editing one that has already run is
detected and refused. Add a new migration instead.

## Legal

Both sites' terms prohibit automated access. This polls your own account, your
own saved list, a handful of times a day. The realistic exposure is
account-level action by the retailer rather than anything legal — worth knowing
before you run it.
