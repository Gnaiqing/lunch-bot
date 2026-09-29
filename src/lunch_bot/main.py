"""Entrypoint: initialise the DB, start the scheduler, and run Socket Mode.

Run with::

    python -m lunch_bot.main

All heavy dependencies (slack-bolt, apscheduler, anthropic) are imported lazily
by the modules this pulls in, so import errors surface only when the app actually
starts — which is the intended behaviour for a long-lived service.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from . import db
from .config import Config, load_config

logger = logging.getLogger(__name__)

# Config attributes that must be set before the bot can start, mapped to the
# environment variable a user actually sets them with.
REQUIRED_SETTINGS = {
    "slack_bot_token": "SLACK_BOT_TOKEN",
    "slack_app_token": "SLACK_APP_TOKEN",
    "slack_signing_secret": "SLACK_SIGNING_SECRET",
    "slack_channel_id": "SLACK_CHANNEL_ID",
}

# The LLM is optional, but when enabled it needs the API key for the ACTIVE
# provider (``config.llm_provider``). Only that provider's key is relevant; the
# other provider's key is never required. Maps provider -> (config attr, env var).
LLM_PROVIDER_KEYS = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
}


def active_llm_api_key(config: Config) -> Optional[str]:
    """Return the API key value for the active LLM provider (or ``None``)."""
    attr, _env = LLM_PROVIDER_KEYS[config.llm_provider]
    return getattr(config, attr, None)


def missing_llm_key_env(config: Config) -> Optional[str]:
    """Return the env-var name of the active provider's key if it is unset.

    The LLM is optional, so a missing key does not stop startup — this just names
    which key to set to enable the LLM. Only the ACTIVE provider's key matters;
    the other provider's key is never required.
    """
    if active_llm_api_key(config):
        return None
    return LLM_PROVIDER_KEYS[config.llm_provider][1]


def main() -> None:
    """Boot the bot: config -> db -> LLM -> Slack app + scheduler -> Socket Mode."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_config()
    missing = [env for attr, env in REQUIRED_SETTINGS.items() if not getattr(config, attr, None)]
    if missing:
        print(
            "lunch-bot: cannot start — missing required configuration.\n"
            "Set the following environment variable(s) (in .env or your shell): "
            + ", ".join(sorted(missing))
            + ".\nSee SETUP.md and .env.example for how to obtain each value.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    conn = db.init_db(config.db_path)
    logger.info("Initialised database at %s", config.db_path)

    # LLM is optional — the bot still runs without it (cuisine tagging + free-text
    # suggestion parsing degrade gracefully). When enabled, only the ACTIVE
    # provider's key is required.
    llm = None
    if active_llm_api_key(config):
        try:
            from .llm import build_llm_client

            llm = build_llm_client(config)
            logger.info("LLM enabled (provider=%s, model=%s)", config.llm_provider, llm.model)
        except Exception as exc:  # pragma: no cover
            logger.warning("LLM unavailable (%s); continuing without it.", exc)
    else:
        logger.info(
            "LLM disabled: no API key for provider %r — set %s to enable "
            "cuisine tagging + suggestion parsing.",
            config.llm_provider, missing_llm_key_env(config),
        )

    from .scheduler import build_scheduler
    from .slack_app import build_app
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app(config, conn, llm=llm)

    scheduler = build_scheduler(config, conn, app.client)
    scheduler.start()
    sched = config.schedule
    logger.info(
        "Scheduler started (poll_create %s %s, poll_close %s %s, order_reminder %s %s; "
        "order_deadline %s %s [human, no job]; tz=%s)",
        sched["poll_create"].day, sched["poll_create"].time_str,
        sched["poll_close"].day, sched["poll_close"].time_str,
        sched["order_reminder"].day, sched["order_reminder"].time_str,
        sched["order_deadline"].day, sched["order_deadline"].time_str,
        config.timezone,
    )

    handler = SocketModeHandler(app, config.slack_app_token)
    logger.info("Starting Slack Socket Mode handler…")
    handler.start()  # blocks forever


if __name__ == "__main__":
    main()
