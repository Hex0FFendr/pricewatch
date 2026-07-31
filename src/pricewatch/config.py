"""Configuration model and loader.

The config file is TOML. Secrets may be given either literally or, preferably,
as an environment-variable reference:

    token = "sk-literal-value"          # works, but ends up in your dotfiles
    token = { env = "PRICEWATCH_NTFY" } # resolved at use time, never on disk

Validation is strict (`extra="forbid"` throughout): a typo in a key name is a
startup error rather than a silently ignored setting. For an unattended daemon
that is the right trade — a mistyped `percent_off` that quietly disables a
trigger would otherwise present as "the tool stopped noticing sales".
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from pricewatch.errors import ConfigError
from pricewatch.logging import LogLevel
from pricewatch.paths import default_state_dir

#: Adapter identifiers. Both are UK storefronts; the tool is .co.uk-only by
#: design, so there is no region axis here.
SiteId = Literal["asos", "freepeople"]

#: Hard floor on polling frequency. Not configurable away: below this the
#: request pattern stops looking like a person checking their saved items.
MIN_POLL_INTERVAL_MINUTES: int = 30


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecretValue(_Strict):
    """A secret supplied either literally or by environment-variable name."""

    value: SecretStr | None = None
    env: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_string(cls, data: Any) -> Any:  # noqa: ANN401
        if isinstance(data, str):
            return {"value": data}
        return data

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.value is None) == (self.env is None):
            raise ValueError('set exactly one of a literal value or `env = "VAR_NAME"`')
        return self

    def resolve(self) -> str:
        """Return the secret, reading the environment if this is a reference.

        Raises `ConfigError` rather than returning None for a missing variable:
        a notifier configured with an unset token is a misconfiguration, and
        discovering that at send time means a missed price alert.
        """
        if self.value is not None:
            return self.value.get_secret_value()
        if self.env is None:  # unreachable: _exactly_one_source guarantees one
            raise ConfigError("secret has neither a literal value nor an env reference")
        resolved = os.environ.get(self.env)
        if not resolved:
            raise ConfigError(f"environment variable {self.env!r} is unset or empty")
        return resolved


class GeneralConfig(_Strict):
    poll_interval_minutes: int = Field(default=180, ge=MIN_POLL_INTERVAL_MINUTES)
    #: Random +/- jitter applied to each cycle so polls do not land on a
    #: predictable clock edge.
    jitter_minutes: int = Field(default=15, ge=0, le=120)
    state_dir: Path = Field(default_factory=default_state_dir)
    #: Defaults to `<state_dir>/pricewatch.db`; see `Config.db_path`.
    db_path: Path | None = None
    log_level: LogLevel = "info"
    #: Consecutive failed cycles before an account is marked `degraded`.
    max_consecutive_failures: int = Field(default=5, ge=1)
    #: Retained raw adapter responses, for debugging a bad parse after the fact.
    raw_response_retention: int = Field(default=20, ge=0)

    @field_validator("state_dir", "db_path")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None


class AccountConfig(_Strict):
    #: Stable identifier used on the CLI and as the session/profile directory
    #: name, so it is constrained to filesystem-safe characters.
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    site: SiteId
    enabled: bool = True


class TriggersConfig(_Strict):
    #: Any drop versus the previous observation.
    any_drop: bool = True
    #: Drop of at least N percent below the effective reference price. `None`
    #: disables. See `pricewatch.triggers` (phase 3) for why the reference is
    #: max(site-reported RRP, highest price we have observed ourselves).
    percent_off: int | None = Field(default=None, ge=1, le=99)
    #: Per-item price targets, set with `pricewatch target <item-id> <price>`.
    target_price: bool = True
    #: Price at or below every price previously recorded for the item.
    lowest_ever: bool = True
    #: Saved variant transitions out-of-stock -> in-stock.
    back_in_stock: bool = True
    #: Ceiling on per-item stock lookups in one cycle, for the case where the
    #: saved-items response does not carry per-variant stock. Only items whose
    #: saved variant is currently out of stock are ever looked up.
    back_in_stock_max_lookups: int = Field(default=10, ge=0)


class _NotifierBase(_Strict):
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    enabled: bool = True


class NtfyNotifier(_NotifierBase):
    type: Literal["ntfy"] = "ntfy"
    server: str = "https://ntfy.sh"
    topic: str = Field(min_length=1)
    token: SecretValue | None = None
    priority: Literal["min", "low", "default", "high", "urgent"] = "default"


class TelegramNotifier(_NotifierBase):
    type: Literal["telegram"] = "telegram"
    bot_token: SecretValue
    chat_id: str = Field(min_length=1)


class DiscordNotifier(_NotifierBase):
    type: Literal["discord"] = "discord"
    #: The webhook URL is itself the credential, hence `SecretValue`.
    webhook_url: SecretValue


class SmtpNotifier(_NotifierBase):
    type: Literal["smtp"] = "smtp"
    host: str = Field(min_length=1)
    port: int = Field(default=587, ge=1, le=65535)
    username: str | None = None
    password: SecretValue | None = None
    from_address: str = Field(min_length=3)
    to_addresses: list[str] = Field(min_length=1)
    starttls: bool = True


Notifier = Annotated[
    NtfyNotifier | TelegramNotifier | DiscordNotifier | SmtpNotifier,
    Field(discriminator="type"),
]


class Config(_Strict):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    accounts: list[AccountConfig] = Field(default_factory=list)
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    notifiers: list[Notifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        for label, names in (
            ("account", [a.name for a in self.accounts]),
            ("notifier", [n.name for n in self.notifiers]),
        ):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            if duplicates:
                raise ValueError(f"duplicate {label} name(s): {', '.join(duplicates)}")
        return self

    @property
    def db_path(self) -> Path:
        if self.general.db_path is not None:
            return self.general.db_path
        return self.general.state_dir / "pricewatch.db"

    @property
    def enabled_accounts(self) -> list[AccountConfig]:
        return [a for a in self.accounts if a.enabled]

    def account(self, name: str) -> AccountConfig:
        for candidate in self.accounts:
            if candidate.name == name:
                return candidate
        known = ", ".join(a.name for a in self.accounts) or "<none configured>"
        raise ConfigError(f"no account named {name!r}; configured accounts: {known}")

    @classmethod
    def load(cls, path: Path) -> Config:
        """Read and validate a config file, or raise `ConfigError`."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read config file {path}: {exc}") from exc

        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

        try:
            return cls.model_validate(parsed)
        except ValidationError as exc:
            raise ConfigError(f"{path}:\n{_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    """Render pydantic's error list as one readable line per problem."""
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)
