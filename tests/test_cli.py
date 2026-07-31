from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from pricewatch.cli import EXIT_NOT_IMPLEMENTED, app

runner = CliRunner()


def everything(result: Result) -> str:
    """All output a user would see, plus any exception, as one string."""
    return f"{result.stdout}{result.stderr}{result.exception or ''}"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\nstate_dir = "{tmp_path / "state"}"\n'
        '\n[[accounts]]\nname = "asos-uk"\nsite = "asos"\n',
        encoding="utf-8",
    )
    return path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Monitor saved items" in result.stdout


def test_config_check_passes(config_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(config_file), "config", "check"])
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout
    assert "asos-uk" in result.stdout


def test_config_check_reports_a_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[general]\npoll_interval_minutes = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["--config", str(bad), "config", "check"])
    assert result.exit_code != 0
    assert "poll_interval_minutes" in everything(result)


def test_config_check_fails_on_unresolvable_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unset token must surface at check time, not when a price finally drops.
    monkeypatch.delenv("PW_MISSING", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[[accounts]]\nname = "asos-uk"\nsite = "asos"\n'
        '\n[[notifiers]]\nname = "phone"\ntype = "ntfy"\ntopic = "t"\n'
        'token = { env = "PW_MISSING" }\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--config", str(path), "config", "check"])
    assert result.exit_code == 1
    assert "PW_MISSING" in result.stdout


def test_init_creates_database_and_syncs_accounts(config_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(config_file), "init"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "state" / "pricewatch.db").exists()
    assert "001_initial" in result.stdout

    accounts = runner.invoke(app, ["--config", str(config_file), "accounts"])
    assert "asos-uk" in accounts.stdout
    assert "unknown" in accounts.stdout


def test_init_is_idempotent(config_file: Path) -> None:
    runner.invoke(app, ["--config", str(config_file), "init"])
    second = runner.invoke(app, ["--config", str(config_file), "init"])
    assert second.exit_code == 0
    assert "already up to date" in second.stdout


def test_db_version_reports_schema(config_file: Path) -> None:
    runner.invoke(app, ["--config", str(config_file), "init"])
    result = runner.invoke(app, ["--config", str(config_file), "db", "version"])
    assert result.stdout.strip() == "1"


def test_missing_config_is_a_clean_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "config", "check"])
    assert result.exit_code != 0
    assert "not found" in everything(result)


@pytest.mark.parametrize(
    "args",
    [
        ["login", "asos-uk"],
        ["discover", "asos-uk"],
        ["run", "--once"],
        ["target", "1", "29.99"],
        ["history", "1"],
    ],
)
def test_unbuilt_commands_exit_distinctly(config_file: Path, args: list[str]) -> None:
    # A dedicated exit code so a script can tell "not built yet" from "failed".
    result = runner.invoke(app, ["--config", str(config_file), *args])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "not implemented yet" in everything(result)


def test_login_validates_the_account_name_first(config_file: Path) -> None:
    # Name errors should be reported even though the command body is unbuilt.
    result = runner.invoke(app, ["--config", str(config_file), "login", "no-such-account"])
    assert result.exit_code != EXIT_NOT_IMPLEMENTED
    assert "no-such-account" in everything(result)
