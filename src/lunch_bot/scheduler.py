"""APScheduler jobs for the weekly lunch workflow.

- **Monday**: build and post the poll (diverse, vote-weighted candidates).
- **Thursday**: close the poll, announce the winner, and post the Uber Eats
  order-prep summary (a human then places the order).

``apscheduler`` and ``slack_sdk`` interactions happen through a Bolt ``app``
client handle passed in; ``apscheduler`` is imported lazily inside
:func:`build_scheduler` so importing this module doesn't require it.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from . import db, polls
from .config import Config
from .selection import select_candidates
from .ubereats import build_order_summary

logger = logging.getLogger(__name__)


def create_weekly_poll(config: Config, conn, client) -> int | None:
    """Select candidates and post the Monday poll to the channel.

    Returns the new poll id, or ``None`` if there was nothing to offer.
    """
    config.require("slack_channel_id")
    active = db.get_active_restaurants(conn)
    if not active:
        logger.warning("No active restaurants in the pool; skipping poll creation.")
        return None

    candidates = select_candidates(
        active,
        n=config.poll_size,
        rng=random.Random(),
        exploration_c=config.exploration_c,
    )
    if not candidates:
        return None

    # Poll closes just before the Thursday reading-group run.
    closes_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    poll_id = db.create_poll(
        conn,
        config.slack_channel_id,
        [c.id for c in candidates if c.id is not None],
        closes_at=closes_at,
    )

    blocks = polls.build_poll_blocks(poll_id, candidates)
    resp = client.chat_postMessage(
        channel=config.slack_channel_id,
        blocks=blocks,
        text="This week's lunch poll is up!",
    )
    ts = resp.get("ts")
    if ts:
        db.set_poll_ts(conn, poll_id, ts)
    logger.info("Posted weekly poll %s with %d options", poll_id, len(candidates))
    return poll_id


def announce_winner_and_prep_order(config: Config, conn, client, *, reading_group_time: str | None = None):
    """Close the open poll, announce the winner, and post Uber Eats prep.

    Also folds the poll's votes into each restaurant's ``total_votes`` so future
    selections are vote-weighted.
    """
    config.require("slack_channel_id")
    open_poll = db.get_open_poll(conn, config.slack_channel_id)
    if not open_poll:
        logger.warning("No open poll to close on Thursday.")
        return
    poll_id = open_poll["id"]

    winner_id = polls.determine_winner(conn, poll_id)
    db.apply_vote_totals_to_pool(conn, poll_id)
    db.close_poll(conn, poll_id, winner_id)

    if winner_id is None:
        client.chat_postMessage(
            channel=config.slack_channel_id,
            text="No votes were cast this week — no lunch winner. 😢",
        )
        return

    winner = db.get_restaurant(conn, winner_id)
    summary = build_order_summary(winner, reading_group_time=reading_group_time)
    client.chat_postMessage(
        channel=config.slack_channel_id,
        blocks=summary["blocks"],
        text=summary["text"],
    )
    logger.info("Announced winner %s for poll %s", winner.name, poll_id)


def build_scheduler(config: Config, conn, client):
    """Create a ``BackgroundScheduler`` with the Monday/Thursday jobs registered.

    The caller is responsible for ``scheduler.start()`` and keeping the process
    alive (see :mod:`lunch_bot.main`).
    """
    from apscheduler.schedulers.background import BackgroundScheduler  # lazy
    from apscheduler.triggers.cron import CronTrigger  # lazy

    scheduler = BackgroundScheduler(timezone=config.timezone)

    scheduler.add_job(
        lambda: create_weekly_poll(config, conn, client),
        CronTrigger(day_of_week="mon", hour=config.monday_hour, minute=0),
        id="monday_poll",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: announce_winner_and_prep_order(config, conn, client),
        CronTrigger(day_of_week="thu", hour=config.thursday_hour, minute=0),
        id="thursday_announce",
        replace_existing=True,
    )
    return scheduler
