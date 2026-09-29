"""Tests for the startup required-settings gate in :mod:`lunch_bot.main`.

No network or credentials needed. These cover the round-2 review fix:

- G1: the bot runs in Socket Mode, so ``SLACK_SIGNING_SECRET`` (an HTTP-receiver
  concern) must NOT be required to start. With the app + bot tokens set the
  required-settings gate passes even with no signing secret; with no creds it
  still fails, flagging the app + bot tokens.
"""

from lunch_bot import main as main_mod
from lunch_bot.config import load_config


def _missing_required(env):
    """Mirror main()'s required-settings computation for a given env mapping."""
    config = load_config(env=env, load_dotenv=False, config_path="__none__.yaml")
    return [
        env_name
        for attr, env_name in main_mod.REQUIRED_SETTINGS.items()
        if not getattr(config, attr, None)
    ]


def test_signing_secret_not_a_required_setting():
    # The signing secret is only for an HTTP receiver, never for Socket Mode.
    assert "slack_signing_secret" not in main_mod.REQUIRED_SETTINGS
    assert "SLACK_SIGNING_SECRET" not in main_mod.REQUIRED_SETTINGS.values()


def test_socket_mode_creds_without_signing_secret_pass():
    # App + bot tokens set, no signing secret; channel id has a default.
    env = {"SLACK_APP_TOKEN": "xapp-x", "SLACK_BOT_TOKEN": "xoxb-x"}
    missing = _missing_required(env)
    assert missing == []
    assert "SLACK_SIGNING_SECRET" not in missing


def test_missing_tokens_still_flagged():
    # No creds at all: app + bot tokens are flagged (channel id has a default),
    # and the signing secret is never flagged.
    missing = _missing_required({})
    assert "SLACK_APP_TOKEN" in missing
    assert "SLACK_BOT_TOKEN" in missing
    assert "SLACK_SIGNING_SECRET" not in missing
    assert "SLACK_CHANNEL_ID" not in missing  # has a default
