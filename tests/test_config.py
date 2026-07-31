from __future__ import annotations

from pathlib import Path

import pytest

from pricewatch.config import Config
from pricewatch.errors import ConfigError

MINIMAL = """
[[accounts]]
name = "asos-uk"
site = "asos"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_loads_with_defaults(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, MINIMAL))
    assert [a.name for a in config.accounts] == ["asos-uk"]
    assert config.general.poll_interval_minutes == 180
    assert config.triggers.any_drop is True
    assert config.triggers.percent_off is None
    assert config.notifiers == []


def test_db_path_defaults_under_state_dir(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, f'[general]\nstate_dir = "{tmp_path}"\n' + MINIMAL))
    assert config.db_path == tmp_path / "pricewatch.db"


def test_explicit_db_path_wins(tmp_path: Path) -> None:
    config = Config.load(
        write(tmp_path, f'[general]\ndb_path = "{tmp_path}/custom.db"\n' + MINIMAL)
    )
    assert config.db_path == tmp_path / "custom.db"


def test_tilde_is_expanded(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, '[general]\nstate_dir = "~/pw"\n' + MINIMAL))
    assert not str(config.general.state_dir).startswith("~")


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        Config.load(tmp_path / "absent.toml")


def test_invalid_toml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        Config.load(write(tmp_path, "this is not = = toml"))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    # A typo must not silently disable a trigger.
    with pytest.raises(ConfigError, match="percent_of"):
        Config.load(write(tmp_path, MINIMAL + "\n[triggers]\npercent_of = 20\n"))


def test_poll_interval_floor_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="poll_interval_minutes"):
        Config.load(write(tmp_path, "[general]\npoll_interval_minutes = 5\n" + MINIMAL))


def test_poll_interval_at_the_floor_is_allowed(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, "[general]\npoll_interval_minutes = 30\n" + MINIMAL))
    assert config.general.poll_interval_minutes == 30


def test_duplicate_account_names_are_rejected(tmp_path: Path) -> None:
    text = MINIMAL + '\n[[accounts]]\nname = "asos-uk"\nsite = "freepeople"\n'
    with pytest.raises(ConfigError, match="duplicate account name"):
        Config.load(write(tmp_path, text))


def test_unknown_site_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="site"):
        Config.load(write(tmp_path, '[[accounts]]\nname = "x"\nsite = "zalando"\n'))


def test_account_name_must_be_filesystem_safe(tmp_path: Path) -> None:
    # Account names become session and profile directory names.
    with pytest.raises(ConfigError, match="name"):
        Config.load(write(tmp_path, '[[accounts]]\nname = "../etc"\nsite = "asos"\n'))


def test_account_lookup_reports_known_names(tmp_path: Path) -> None:
    config = Config.load(write(tmp_path, MINIMAL))
    assert config.account("asos-uk").site == "asos"
    with pytest.raises(ConfigError, match="asos-uk"):
        config.account("nope")


def test_enabled_accounts_filters(tmp_path: Path) -> None:
    text = MINIMAL + '\n[[accounts]]\nname = "fp-uk"\nsite = "freepeople"\nenabled = false\n'
    config = Config.load(write(tmp_path, text))
    assert [a.name for a in config.enabled_accounts] == ["asos-uk"]


class TestSecrets:
    ntfy = MINIMAL + '\n[[notifiers]]\nname = "phone"\ntype = "ntfy"\ntopic = "pw"\n'

    def test_literal_secret(self, tmp_path: Path) -> None:
        config = Config.load(write(tmp_path, self.ntfy + 'token = "tk_literal"\n'))
        notifier = config.notifiers[0]
        assert notifier.type == "ntfy"
        assert notifier.token is not None
        assert notifier.token.resolve() == "tk_literal"

    def test_secret_is_not_in_the_repr(self, tmp_path: Path) -> None:
        config = Config.load(write(tmp_path, self.ntfy + 'token = "tk_literal"\n'))
        assert "tk_literal" not in repr(config)

    def test_env_reference(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PW_TEST_TOKEN", "tk_from_env")
        config = Config.load(write(tmp_path, self.ntfy + 'token = { env = "PW_TEST_TOKEN" }\n'))
        notifier = config.notifiers[0]
        assert notifier.type == "ntfy"
        assert notifier.token is not None
        assert notifier.token.resolve() == "tk_from_env"

    def test_unset_env_var_raises_at_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PW_TEST_TOKEN", raising=False)
        config = Config.load(write(tmp_path, self.ntfy + 'token = { env = "PW_TEST_TOKEN" }\n'))
        notifier = config.notifiers[0]
        assert notifier.type == "ntfy"
        assert notifier.token is not None
        with pytest.raises(ConfigError, match="PW_TEST_TOKEN"):
            notifier.token.resolve()

    def test_both_sources_is_rejected(self, tmp_path: Path) -> None:
        text = self.ntfy + 'token = { value = "a", env = "B" }\n'
        with pytest.raises(ConfigError, match="exactly one"):
            Config.load(write(tmp_path, text))

    def test_neither_source_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            Config.load(write(tmp_path, self.ntfy + "token = {}\n"))


class TestNotifiers:
    def test_discriminated_union_selects_the_right_model(self, tmp_path: Path) -> None:
        text = (
            MINIMAL
            + """
[[notifiers]]
name = "phone"
type = "ntfy"
topic = "pw-alerts"

[[notifiers]]
name = "tg"
type = "telegram"
bot_token = "123:abc"
chat_id = "456"

[[notifiers]]
name = "mail"
type = "smtp"
host = "smtp.example.co.uk"
from_address = "pw@example.co.uk"
to_addresses = ["me@example.co.uk"]
"""
        )
        config = Config.load(write(tmp_path, text))
        assert [n.type for n in config.notifiers] == ["ntfy", "telegram", "smtp"]

    def test_missing_required_field_names_the_field(self, tmp_path: Path) -> None:
        text = MINIMAL + '\n[[notifiers]]\nname = "phone"\ntype = "ntfy"\n'
        with pytest.raises(ConfigError, match="topic"):
            Config.load(write(tmp_path, text))

    def test_unknown_notifier_type_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL + '\n[[notifiers]]\nname = "x"\ntype = "carrier-pigeon"\n'
        with pytest.raises(ConfigError, match="type"):
            Config.load(write(tmp_path, text))

    def test_duplicate_notifier_names_are_rejected(self, tmp_path: Path) -> None:
        text = (
            MINIMAL
            + """
[[notifiers]]
name = "phone"
type = "ntfy"
topic = "a"

[[notifiers]]
name = "phone"
type = "ntfy"
topic = "b"
"""
        )
        with pytest.raises(ConfigError, match="duplicate notifier name"):
            Config.load(write(tmp_path, text))

    def test_smtp_requires_at_least_one_recipient(self, tmp_path: Path) -> None:
        text = (
            MINIMAL
            + """
[[notifiers]]
name = "mail"
type = "smtp"
host = "smtp.example.co.uk"
from_address = "pw@example.co.uk"
to_addresses = []
"""
        )
        with pytest.raises(ConfigError, match="to_addresses"):
            Config.load(write(tmp_path, text))


class TestTriggers:
    def test_percent_off_bounds(self, tmp_path: Path) -> None:
        for value in (0, 100):
            with pytest.raises(ConfigError, match="percent_off"):
                Config.load(write(tmp_path, MINIMAL + f"\n[triggers]\npercent_off = {value}\n"))

    def test_percent_off_accepted(self, tmp_path: Path) -> None:
        config = Config.load(write(tmp_path, MINIMAL + "\n[triggers]\npercent_off = 20\n"))
        assert config.triggers.percent_off == 20

    def test_back_in_stock_lookup_cap_defaults_bounded(self, tmp_path: Path) -> None:
        config = Config.load(write(tmp_path, MINIMAL))
        assert config.triggers.back_in_stock_max_lookups == 10
