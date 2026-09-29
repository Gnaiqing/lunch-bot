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

from . import db
from .config import load_config

logger = logging.getLogger(__name__)

# Config attributes that must be set before the bot can start, mapped to the
# environment variable a user actually sets them with.
REQUIRED_SETTINGS = {
    "slack_bot_token": "SLACK_BOT_TOKEN",
    "slack_app_token": "SLACK_APP_TOKEN",
    "slack_signing_secret": "SLACK_SIGNING_SECRET",
    "slack_channel_id": "SLACK_CHANNEL_ID",
}


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
    # suggestion parsing degrade gracefully).
    llm = None
    if config.anthropic_api_key:
        try:
            from .llm import LLMClient

            llm = LLMClient(api_key=config.anthropic_api_key, model=config.llm_model)
            logger.info("LLM enabled (model=%s)", config.llm_model)
        except Exception as exc:  # pragma: no cover
            logger.warning("LLM unavailable (%s); continuing without it.", exc)

    from .scheduler import build_scheduler
    from .slack_app import build_app
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app(config, conn, llm=llm)

    scheduler = build_scheduler(config, conn, app.client)
    scheduler.start()
    logger.info("Scheduler started (Monday poll @ %02d:00, Thursday announce @ %02d:00, tz=%s)",
                config.monday_hour, config.thursday_hour, config.timezone)

    handler = SocketModeHandler(app, config.slack_app_token)
    logger.info("Starting Slack Socket Mode handler…")
    handler.start()  # blocks forever


if __name__ == "__main__":
    main()
