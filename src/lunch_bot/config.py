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
DEFAULT_LLM_MODEL = "claude-haiku-4-5"  # small/fast model for tagging + parsing


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

    # --- Non-secret knobs (env or yaml) ---
    slack_channel_id: Optional[str] = None
    office_address: str = DEFAULT_OFFICE_ADDRESS
    office_lat: float = DEFAULT_OFFICE_LAT
    office_lng: float = DEFAULT_OFFICE_LNG
    search_radius_m: int = DEFAULT_SEARCH_RADIUS_M
    poll_size: int = DEFAULT_POLL_SIZE
    max_price_level: int = DEFAULT_MAX_PRICE_LEVEL
    exploration_c: float = DEFAULT_EXPLORATION_C
    db_path: str = DEFAULT_DB_PATH
    llm_model: str = DEFAULT_LLM_MODEL

    # Scheduling (cron-ish). Times are local to ``timezone``.
    timezone: str = "America/Toronto"
    monday_hour: int = 10  # Monday poll creation hour (24h)
    thursday_hour: int = 11  # Thursday announce + order-prep hour (24h)

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

    return Config(
        # Secrets: env only.
        slack_bot_token=env.get("SLACK_BOT_TOKEN"),
        slack_app_token=env.get("SLACK_APP_TOKEN"),
        slack_signing_secret=env.get("SLACK_SIGNING_SECRET"),
        google_maps_api_key=env.get("GOOGLE_MAPS_API_KEY"),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY"),
        # Non-secret knobs: env > yaml > default.
        slack_channel_id=pick("SLACK_CHANNEL_ID", "slack_channel_id", None),
        office_address=pick("OFFICE_ADDRESS", "office_address", DEFAULT_OFFICE_ADDRESS),
        office_lat=_as_float(pick("OFFICE_LAT", "office_lat", DEFAULT_OFFICE_LAT), DEFAULT_OFFICE_LAT),
        office_lng=_as_float(pick("OFFICE_LNG", "office_lng", DEFAULT_OFFICE_LNG), DEFAULT_OFFICE_LNG),
        search_radius_m=_as_int(pick("SEARCH_RADIUS_M", "search_radius_m", DEFAULT_SEARCH_RADIUS_M), DEFAULT_SEARCH_RADIUS_M),
        poll_size=_as_int(pick("POLL_SIZE", "poll_size", DEFAULT_POLL_SIZE), DEFAULT_POLL_SIZE),
        max_price_level=_as_int(pick("MAX_PRICE_LEVEL", "max_price_level", DEFAULT_MAX_PRICE_LEVEL), DEFAULT_MAX_PRICE_LEVEL),
        exploration_c=_as_float(pick("EXPLORATION_C", "exploration_c", DEFAULT_EXPLORATION_C), DEFAULT_EXPLORATION_C),
        db_path=pick("DB_PATH", "db_path", DEFAULT_DB_PATH),
        llm_model=pick("LLM_MODEL", "llm_model", DEFAULT_LLM_MODEL),
        timezone=pick("TIMEZONE", "timezone", "America/Toronto"),
        monday_hour=_as_int(pick("MONDAY_HOUR", "monday_hour", 10), 10),
        thursday_hour=_as_int(pick("THURSDAY_HOUR", "thursday_hour", 11), 11),
        extra=yaml_cfg,
    )
