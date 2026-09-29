"""Tests for configuration loading (defaults, env overrides, yaml precedence).

No network or credentials needed.
"""

import textwrap

from lunch_bot.config import (
    DEFAULT_MAX_PRICE_LEVEL,
    DEFAULT_OFFICE_ADDRESS,
    DEFAULT_POLL_SIZE,
    DEFAULT_SEARCH_RADIUS_M,
    Config,
    load_config,
)


def test_defaults_with_empty_env(tmp_path):
    cfg = load_config(config_path=str(tmp_path / "nope.yaml"), env={}, load_dotenv=False)
    assert cfg.office_address == DEFAULT_OFFICE_ADDRESS
    assert cfg.search_radius_m == DEFAULT_SEARCH_RADIUS_M
    assert cfg.poll_size == DEFAULT_POLL_SIZE
    assert cfg.max_price_level == DEFAULT_MAX_PRICE_LEVEL
    assert cfg.slack_bot_token is None
    assert cfg.llm_model == "claude-haiku-4-5"


def test_env_overrides_defaults(tmp_path):
    env = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SEARCH_RADIUS_M": "8000",
        "POLL_SIZE": "6",
        "MAX_PRICE_LEVEL": "1",
        "OFFICE_LAT": "40.0",
        "OFFICE_LNG": "-70.0",
    }
    cfg = load_config(config_path=str(tmp_path / "nope.yaml"), env=env, load_dotenv=False)
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.search_radius_m == 8000
    assert cfg.poll_size == 6
    assert cfg.max_price_level == 1
    assert cfg.office_lat == 40.0
    assert cfg.office_lng == -70.0


def test_env_beats_yaml(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            office_address: "From YAML Ave"
            search_radius_m: 3000
            poll_size: 4
            """
        )
    )
    # Env overrides the yaml value; yaml overrides the default.
    env = {"SEARCH_RADIUS_M": "9000"}
    cfg = load_config(config_path=str(yaml_file), env=env, load_dotenv=False)
    assert cfg.office_address == "From YAML Ave"  # from yaml
    assert cfg.search_radius_m == 9000  # env beats yaml
    assert cfg.poll_size == 4  # from yaml


def test_yaml_missing_is_tolerated(tmp_path):
    cfg = load_config(config_path=str(tmp_path / "absent.yaml"), env={}, load_dotenv=False)
    assert isinstance(cfg, Config)
    assert cfg.poll_size == DEFAULT_POLL_SIZE


def test_require_raises_on_missing():
    cfg = load_config(env={}, load_dotenv=False, config_path="__none__.yaml")
    try:
        cfg.require("slack_bot_token", "slack_app_token")
    except ValueError as exc:
        assert "slack_bot_token" in str(exc)
        assert "slack_app_token" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_require_passes_when_present():
    cfg = load_config(env={"SLACK_BOT_TOKEN": "x"}, load_dotenv=False, config_path="__none__.yaml")
    cfg.require("slack_bot_token")  # should not raise
