"""Command-line interface.

Commands that are not yet implemented are still registered, deliberately: the
CLI surface is part of the design, and a command that exits with "arrives in
phase N" is more useful than one that does not exist and reports a typo.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pricewatch import __version__
from pricewatch.accounts import list_accounts, sync_accounts
from pricewatch.config import Config
from pricewatch.db import current_version, migrate, open_database
from pricewatch.errors import PricewatchError
from pricewatch.logging import configure_logging
from pricewatch.paths import default_config_path

app = typer.Typer(
    name="pricewatch",
    help="Monitor saved items on UK retail accounts for price drops and restocks.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")

#: Exit code for "the command exists but this phase has not built it yet".
EXIT_NOT_IMPLEMENTED = 3


@dataclass(slots=True)
class AppContext:
    config_path: Path
    _config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config.load(self.config_path)
            configure_logging(self._config.general.log_level)
        return self._config

    def database(self) -> sqlite3.Connection:
        return open_database(self.config.db_path)

    def require_config(self) -> None:
        """Load and validate the config, discarding the result.

        Used by commands whose bodies are not built yet: a broken config should
        still be reported as a config error rather than shadowed by the
        not-implemented exit.
        """
        _ = self.config


def _ctx(ctx: typer.Context) -> AppContext:
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - callback always sets it
        raise PricewatchError("CLI context was not initialised")
    return obj


def _not_implemented(command: str, phase: int, needs: str) -> NoReturn:
    typer.secho(
        f"`pricewatch {command}` is not implemented yet (planned for phase {phase}).\n{needs}",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


@app.callback()
def main_callback(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config.toml.", envvar="PRICEWATCH_CONFIG"),
    ] = None,
) -> None:
    ctx.obj = AppContext(config_path=(config_path or default_config_path()).expanduser())


@app.command()
def version() -> None:
    """Print the pricewatch version."""
    typer.echo(__version__)


@config_app.command("path")
def config_path_cmd(ctx: typer.Context) -> None:
    """Print the config file path that would be used."""
    typer.echo(str(_ctx(ctx).config_path))


@config_app.command("check")
def config_check(ctx: typer.Context) -> None:
    """Validate the config file and print the resolved settings."""
    app_ctx = _ctx(ctx)
    config = app_ctx.config

    typer.secho(f"OK  {app_ctx.config_path}", fg=typer.colors.GREEN)
    typer.echo(f"  state dir       {config.general.state_dir}")
    typer.echo(f"  database        {config.db_path}")
    typer.echo(
        f"  poll interval   {config.general.poll_interval_minutes}m "
        f"(+/- {config.general.jitter_minutes}m jitter)"
    )

    typer.echo(f"  accounts        {len(config.accounts)}")
    for account in config.accounts:
        state = "enabled" if account.enabled else "disabled"
        typer.echo(f"    - {account.name} [{account.site}] {state}")

    triggers = config.triggers
    active = [
        name
        for name in ("any_drop", "target_price", "lowest_ever", "back_in_stock")
        if getattr(triggers, name)
    ]
    if triggers.percent_off is not None:
        active.append(f"percent_off>={triggers.percent_off}%")
    typer.echo(f"  triggers        {', '.join(active) or 'none'}")

    typer.echo(f"  notifiers       {len(config.notifiers)}")
    for notifier in config.notifiers:
        state = "enabled" if notifier.enabled else "disabled"
        typer.echo(f"    - {notifier.name} [{notifier.type}] {state}")

    # Resolve every secret now rather than at 3am when a price actually drops.
    unresolved: list[str] = []
    for notifier in config.notifiers:
        if not notifier.enabled:
            continue
        for field in ("token", "bot_token", "webhook_url", "password"):
            secret = getattr(notifier, field, None)
            if secret is None:
                continue
            try:
                secret.resolve()
            except PricewatchError as exc:
                unresolved.append(f"    - {notifier.name}.{field}: {exc}")

    if unresolved:
        typer.secho("  secrets         UNRESOLVED", fg=typer.colors.RED)
        for line in unresolved:
            typer.secho(line, fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo("  secrets         all resolved")


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the state directory and database, and apply migrations."""
    app_ctx = _ctx(ctx)
    config = app_ctx.config
    config.general.state_dir.mkdir(parents=True, exist_ok=True)

    conn = open_database(config.db_path, migrate_to_latest=False)
    applied = migrate(conn)
    sync_accounts(conn, config)

    typer.secho(f"database  {config.db_path}", fg=typer.colors.GREEN)
    if applied:
        for migration in applied:
            typer.echo(f"  applied {migration.version:03d}_{migration.name}")
    else:
        typer.echo("  schema already up to date")
    typer.echo(f"  {len(config.accounts)} account(s) synced")
    conn.close()


@db_app.command("version")
def db_version(ctx: typer.Context) -> None:
    """Print the applied schema version."""
    conn = open_database(_ctx(ctx).config.db_path, migrate_to_latest=False)
    typer.echo(str(current_version(conn)))
    conn.close()


@db_app.command("upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply any pending migrations."""
    conn = open_database(_ctx(ctx).config.db_path, migrate_to_latest=False)
    applied = migrate(conn)
    if applied:
        for migration in applied:
            typer.secho(f"applied {migration.version:03d}_{migration.name}", fg=typer.colors.GREEN)
    else:
        typer.echo("already up to date")
    conn.close()


@app.command("accounts")
def accounts_cmd(ctx: typer.Context) -> None:
    """List accounts and their session health."""
    app_ctx = _ctx(ctx)
    conn = app_ctx.database()
    sync_accounts(conn, app_ctx.config)
    states = list_accounts(conn)
    conn.close()

    if not states:
        typer.echo("No accounts configured.")
        return

    colours = {
        "ok": typer.colors.GREEN,
        "needs_reauth": typer.colors.RED,
        "degraded": typer.colors.YELLOW,
        "unknown": typer.colors.WHITE,
    }
    header = f"{'ACCOUNT':<20} {'SITE':<12} {'STATUS':<14} {'MODE':<8} {'ITEMS':>5}  LAST OK"
    typer.echo(header)
    for state in states:
        status = state.status if state.enabled else "disabled"
        typer.secho(
            f"{state.name:<20} {state.site:<12} {status:<14} "
            f"{state.last_exec_mode or '-':<8} {state.tracked_items:>5}  "
            f"{state.last_ok_at or 'never'}",
            fg=colours.get(status, typer.colors.WHITE),
        )
        if state.needs_attention and state.last_error:
            typer.secho(f"{'':<20} last error: {state.last_error}", fg=typer.colors.YELLOW)


@app.command()
def login(
    ctx: typer.Context,
    account: Annotated[str, typer.Argument(help="Account name from the config file.")],
) -> None:
    """Open a browser to sign in and capture the session."""
    _ctx(ctx).config.account(account)  # validate the name before failing on phase
    _not_implemented(
        "login",
        2,
        "Needs the session store and a headed browser run against your account.",
    )


@app.command()
def discover(
    ctx: typer.Context,
    account: Annotated[str, typer.Argument(help="Account name from the config file.")],
) -> None:
    """Record the saved-items network traffic so an adapter can be written."""
    _ctx(ctx).config.account(account)
    _not_implemented(
        "discover",
        2,
        "Needs a signed-in session; output is redacted at capture time.",
    )


@app.command()
def run(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once", help="Poll a single cycle and exit.")] = False,
) -> None:
    """Run the polling loop."""
    _ctx(ctx).require_config()
    _not_implemented(
        "run" + (" --once" if once else ""),
        5,
        "Needs the adapters (phases 6-7) before it has anything to poll.",
    )


@app.command()
def target(
    ctx: typer.Context,
    item_id: Annotated[int, typer.Argument(help="Item id from `pricewatch items`.")],
    price: Annotated[str, typer.Argument(help="Target price, e.g. 29.99. 'none' clears it.")],
) -> None:
    """Set or clear a per-item target price."""
    _ctx(ctx).require_config()
    _not_implemented(f"target {item_id} {price}", 3, "Needs the item store and trigger engine.")


@app.command()
def history(
    ctx: typer.Context,
    item_id: Annotated[int, typer.Argument(help="Item id from `pricewatch items`.")],
) -> None:
    """Show recorded price history for one item."""
    _ctx(ctx).require_config()
    _not_implemented(f"history {item_id}", 3, "Needs the observation store.")


def main() -> None:
    """Console-script entry point with uniform error handling."""
    try:
        app()
    except PricewatchError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
