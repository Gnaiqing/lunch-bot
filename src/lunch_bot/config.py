"""Configuration loading: environment variables + optional ``config.yaml``.

Secrets come from environment variables (optionally via a ``.env`` file loaded
with ``python-dotenv``). Non-secret knobs (office location, radius, poll size,
etc.) can live in a ``config.yaml`` and be overridden by environment variables.

The module avoids importing optional third-party packages at import time:
``dotenv`` and ``yaml`` are imported lazily inside :func:`load_config` and are
optional — if they are not installed, loading still works from the process
environment and built-in defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Defaults (non-secret). These match the decisions made with the user.
# ---------------------------------------------------------------------------
DEFAULT_OFFICE_ADDRESS = "661 University Ave, Toronto"
# Approximate coordinates of the office; overridable via config/env or geocoding.
DEFAULT_OFFICE_LAT = 43.6579
DEFAULT_OFFICE_LNG = -79.3883
DEFAULT_SEARCH_RADIUS_M = 5000  # 5 km
DEFAULT_POLL_SIZE = 5  # candidates per poll; keep within 4..6
DEFAULT_MAX_PRICE_LEVEL = 2  # Google price_level 0..4; <=2 ~ "economic" (<= $30 pp)
DEFAULT_EXPLORATION_C = 1.0
DEFAULT_DB_PATH = "lunch_bot.db"
# LLM provider selection + per-provider defaults. Only the selected provider's
# API key is required (validated just-in-time). Anthropic uses the current
# Haiku-class model; OpenAI uses a small, cheap current model.
LLM_PROVIDERS = ("anthropic", "openai")
DEFAULT_LLM_PROVIDER = "anthropic"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"  # small/fast model for tagging + parsing
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"  # small/cheap model for tagging + parsing
DEFAULT_RESTAURANTS_CSV = "data/restaurants_seed.csv"  # candidate source for seeding

# Channels. The PRODUCTION channel is #dl-time-series-tabular; #thoughts-on-lunch
# (C0C4DT9J75Z) is the TEST channel — set SLACK_CHANNEL_ID to it while testing.
DEFAULT_SLACK_CHANNEL_ID = "C03J1AVLGFM"  # #dl-time-series-tabular (production)
DEFAULT_SLACK_CHANNEL_NAME = "#dl-time-series-tabular"

# Scheduling. Times are local to ``timezone``. ``order_deadline`` is
# informational (used in the reminder text): the organizer places the order then,
# so it is NOT a scheduled bot job.
DEFAULT_TIMEZONE = "America/Toronto"
DEFAULT_SCHEDULE: dict[str, dict[str, str]] = {
    "poll_create": {"day": "mon", "time": "10:00"},
    "poll_close": {"day": "wed", "time": "10:00"},  # close poll + announce winner
    "order_reminder": {"day": "thu", "time": "10:00"},
    "order_deadline": {"day": "thu", "time": "11:00"},  # informational only
}

# Canonical APScheduler day_of_week tokens plus full-name aliases.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _parse_provider(value: Any, *, field_name: str = "llm_provider") -> str:
    """Normalise + validate the LLM provider (``anthropic`` or ``openai``).

    Raises ``ValueError`` with a clear message on an unknown value.
    """
    key = str(value).strip().lower()
    if key in LLM_PROVIDERS:
        return key
    raise ValueError(
        f"Invalid {field_name}: {value!r}. Use one of {', '.join(LLM_PROVIDERS)}."
    )


def _parse_day(value: Any, *, field_name: str) -> str:
    """Normalise a weekday to an APScheduler token (``mon``..``sun``).

    Accepts short (``mon``) or full (``monday``) names, case-insensitively.
    Raises ``ValueError`` with a clear message on anything else.
    """
    key = str(value).strip().lower()
    if key in _WEEKDAYS:
        return key
    if key in _DAY_ALIASES:
        return _DAY_ALIASES[key]
    raise ValueError(
        f"Invalid weekday for {field_name}: {value!r}. "
        f"Use one of {', '.join(_WEEKDAYS)} (or full names like 'monday')."
    )


def _parse_time(value: Any, *, field_name: str) -> tuple[int, int]:
    """Parse an ``"HH:MM"`` string into ``(hour, minute)``.

    Raises ``ValueError`` with a clear message on a malformed value or an
    out-of-range hour/minute.
    """
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid time for {field_name}: {value!r}. Expected 'HH:MM' (24-hour)."
        )
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(
            f"Invalid time for {field_name}: {value!r}. Expected 'HH:MM' (24-hour)."
        ) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            f"Invalid time for {field_name}: {value!r}. "
            "Hour must be 0-23 and minute 0-59."
        )
    return hour, minute


@dataclass
class ScheduleEntry:
    """A single scheduled phase: a weekday and a local time.

    ``day`` is an APScheduler ``day_of_week`` token (``mon``..``sun``).
    """

    day: str
    hour: int
    minute: int

    @property
    def time_str(self) -> str:
        """The ``"HH:MM"`` rendering, e.g. for reminder/log text."""
        return f"{self.hour:02d}:{self.minute:02d}"


def _build_schedule(yaml_cfg: dict) -> dict[str, ScheduleEntry]:
    """Build the phase -> :class:`ScheduleEntry` map from yaml over defaults.

    Each phase (and each ``day``/``time`` within it) falls back to
    :data:`DEFAULT_SCHEDULE`. Raises ``ValueError`` on malformed input.
    """
    raw = yaml_cfg.get("schedule") or {}
    if not isinstance(raw, dict):
        raise ValueError("config 'schedule' must be a mapping of phase -> {day, time}.")

    schedule: dict[str, ScheduleEntry] = {}
    for phase, default in DEFAULT_SCHEDULE.items():
        entry = raw.get(phase)
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise ValueError(
                f"schedule.{phase} must be a mapping with 'day' and 'time'."
            )
        day = _parse_day(entry.get("day", default["day"]), field_name=f"schedule.{phase}.day")
        hour, minute = _parse_time(
            entry.get("time", default["time"]), field_name=f"schedule.{phase}.time"
        )
        schedule[phase] = ScheduleEntry(day=day, hour=hour, minute=minute)
    return schedule


@dataclass
class Config:
    """Resolved configuration for the bot.

    Secrets default to ``None`` when unset so callers can validate what they need
    just-in-time (e.g. Slack tokens are only required to actually start the app).
    """

    # --- Secrets (from env) ---
    slack_bot_token: Optional[str] = None
    slack_app_token: Optional[str] = None
    slack_signing_secret: Optional[str] = None
    google_maps_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None

    # --- Non-secret knobs (env or yaml) ---
    slack_channel_id: Optional[str] = DEFAULT_SLACK_CHANNEL_ID
    slack_channel_name: str = DEFAULT_SLACK_CHANNEL_NAME  # human-readable, for messages
    office_address: str = DEFAULT_OFFICE_ADDRESS
    office_lat: float = DEFAULT_OFFICE_LAT
    office_lng: float = DEFAULT_OFFICE_LNG
    search_radius_m: int = DEFAULT_SEARCH_RADIUS_M
    poll_size: int = DEFAULT_POLL_SIZE
    max_price_level: int = DEFAULT_MAX_PRICE_LEVEL
    exploration_c: float = DEFAULT_EXPLORATION_C
    db_path: str = DEFAULT_DB_PATH
    # LLM provider selection + per-provider models. Only the selected provider's
    # API key is required (see main.py's startup validation).
    llm_provider: str = DEFAULT_LLM_PROVIDER
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL
    restaurants_csv: str = DEFAULT_RESTAURANTS_CSV  # candidate source for seeding

    # Scheduling. Times are local to ``timezone``. ``schedule`` maps each phase
    # (poll_create/poll_close/order_reminder/order_deadline) to a ScheduleEntry;
    # ``order_deadline`` is informational only (no bot job).
    timezone: str = DEFAULT_TIMEZONE
    schedule: dict = field(default_factory=lambda: _build_schedule({}))

    # Raw yaml contents for anything not explicitly modelled here.
    extra: dict = field(default_factory=dict)

    def require(self, *names: str) -> None:
        """Raise ``ValueError`` if any named attribute is unset/empty.

        Use at the point a secret is actually needed, e.g.
        ``config.require("slack_bot_token", "slack_app_token")``.
        """
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise ValueError(
                "Missing required configuration: " + ", ".join(sorted(missing))
            )


def _load_yaml(path: str) -> dict:
    """Load a YAML file into a dict, tolerating a missing file or missing PyYAML."""
    if not path or not os.path.exists(path):
        return {}
    try:
        import yaml  # imported lazily; optional dependency
    except ImportError:  # pragma: no cover - depends on environment
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def load_config(
    config_path: Optional[str] = None,
    *,
    env: Optional[dict] = None,
    load_dotenv: bool = True,
) -> Config:
    """Build a :class:`Config` from ``config.yaml`` + environment variables.

    Precedence (highest first): environment variables > ``config.yaml`` values >
    built-in defaults.

    Args:
        config_path: Path to a YAML file of non-secret settings. Defaults to the
            ``LUNCH_BOT_CONFIG`` env var or ``config.yaml`` in the CWD. A missing
            file (or missing PyYAML) is treated as "no yaml".
        env: Mapping to read variables from (defaults to ``os.environ``). Useful
            for tests.
        load_dotenv: When ``True`` and ``python-dotenv`` is installed, load a
            ``.env`` file into the process environment first. Ignored if ``env``
            is provided explicitly.
    """
    if env is None:
        if load_dotenv:
            try:
                from dotenv import load_dotenv as _dotenv_load  # lazy optional

                _dotenv_load()
            except ImportError:  # pragma: no cover - depends on environment
                pass
        env = os.environ

    if config_path is None:
        config_path = env.get("LUNCH_BOT_CONFIG", "config.yaml")

    yaml_cfg = _load_yaml(config_path)

    def pick(env_key: str, yaml_key: str, default: Any) -> Any:
        """Env var wins, then yaml, then default."""
        if env_key in env and env[env_key] != "":
            return env[env_key]
        if yaml_key in yaml_cfg and yaml_cfg[yaml_key] is not None:
            return yaml_cfg[yaml_key]
        return default

    def pick_first(env_keys: list[str], yaml_keys: list[str], default: Any) -> Any:
        """Like :func:`pick` but tries several env/yaml keys in order.

        Lets a renamed setting keep honouring its legacy name (e.g.
        ``ANTHROPIC_MODEL`` with a ``LLM_MODEL`` fallback).
        """
        for env_key in env_keys:
            if env_key in env and env[env_key] != "":
                return env[env_key]
        for yaml_key in yaml_keys:
            if yaml_key in yaml_cfg and yaml_cfg[yaml_key] is not None:
                return yaml_cfg[yaml_key]
        return default

    return Config(
        # Secrets: env only.
        slack_bot_token=env.get("SLACK_BOT_TOKEN"),
        slack_app_token=env.get("SLACK_APP_TOKEN"),
        slack_signing_secret=env.get("SLACK_SIGNING_SECRET"),
        google_maps_api_key=env.get("GOOGLE_MAPS_API_KEY"),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY"),
        openai_api_key=env.get("OPENAI_API_KEY"),
        # Non-secret knobs: env > yaml > default.
        slack_channel_id=pick("SLACK_CHANNEL_ID", "slack_channel_id", DEFAULT_SLACK_CHANNEL_ID),
        slack_channel_name=pick("SLACK_CHANNEL_NAME", "slack_channel_name", DEFAULT_SLACK_CHANNEL_NAME),
        office_address=pick("OFFICE_ADDRESS", "office_address", DEFAULT_OFFICE_ADDRESS),
        office_lat=_as_float(pick("OFFICE_LAT", "office_lat", DEFAULT_OFFICE_LAT), DEFAULT_OFFICE_LAT),
        office_lng=_as_float(pick("OFFICE_LNG", "office_lng", DEFAULT_OFFICE_LNG), DEFAULT_OFFICE_LNG),
        search_radius_m=_as_int(pick("SEARCH_RADIUS_M", "search_radius_m", DEFAULT_SEARCH_RADIUS_M), DEFAULT_SEARCH_RADIUS_M),
        poll_size=_as_int(pick("POLL_SIZE", "poll_size", DEFAULT_POLL_SIZE), DEFAULT_POLL_SIZE),
        max_price_level=_as_int(pick("MAX_PRICE_LEVEL", "max_price_level", DEFAULT_MAX_PRICE_LEVEL), DEFAULT_MAX_PRICE_LEVEL),
        exploration_c=_as_float(pick("EXPLORATION_C", "exploration_c", DEFAULT_EXPLORATION_C), DEFAULT_EXPLORATION_C),
        db_path=pick("DB_PATH", "db_path", DEFAULT_DB_PATH),
        llm_provider=_parse_provider(pick("LLM_PROVIDER", "llm_provider", DEFAULT_LLM_PROVIDER)),
        # ``ANTHROPIC_MODEL``/``anthropic_model`` with a legacy ``LLM_MODEL``/``llm_model`` fallback.
        anthropic_model=pick_first(
            ["ANTHROPIC_MODEL", "LLM_MODEL"], ["anthropic_model", "llm_model"], DEFAULT_ANTHROPIC_MODEL
        ),
        openai_model=pick("OPENAI_MODEL", "openai_model", DEFAULT_OPENAI_MODEL),
        restaurants_csv=pick("RESTAURANTS_CSV", "restaurants_csv", DEFAULT_RESTAURANTS_CSV),
        timezone=pick("TIMEZONE", "timezone", DEFAULT_TIMEZONE),
        schedule=_build_schedule(yaml_cfg),
        extra=yaml_cfg,
    )
