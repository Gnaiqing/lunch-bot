"""APScheduler jobs for the weekly lunch workflow.

Three configurable bot jobs (all times local to ``config.timezone``):

- **Poll create** (default Mon 10:00): build and post the poll (diverse,
  vote-weighted candidates), opening voting.
- **Poll close + announce** (default Wed 10:00): close the poll, tally + record
  votes, announce the winner, and prompt the ORGANIZER to create and post the
  Uber Eats group-order link (a suggested search is included to save a lookup).
- **Order reminder** (default Thu 10:00): remind the group to place their orders
  on the group-order link before the organizer's deadline (``order_deadline``,
  default Thu 11:00 — a HUMAN step, so there is no bot job for it).

The bot NEVER creates or places the order.

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
    """Select candidates and post the poll-create-day poll to the channel.

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

    # Poll closes on the configured poll_close day (Mon -> Wed by default).
    closes_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
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


def close_poll_and_announce(config: Config, conn, client, *, reading_group_time: str | None = None):
    """Close the open poll, record votes, announce the winner, and prompt prep.

    Folds the poll's votes into each restaurant's ``total_votes`` (and
    ``times_selected``) so future selections are vote-weighted (preference
    memory). Then posts the winner and prompts the ORGANIZER to create + post the
    Uber Eats group-order link (with a suggested search to save a lookup). The
    bot never creates or places the order.
    """
    config.require("slack_channel_id")
    open_poll = db.get_open_poll(conn, config.slack_channel_id)
    if not open_poll:
        logger.warning("No open poll to close + announce.")
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


def send_order_reminder(config: Config, conn, client):
    """Post the order-day reminder pinging the group to place their orders.

    References the ``order_deadline`` cutoff, at which the organizer (a HUMAN)
    closes the group-order link and places the order. There is no bot job for the
    deadline itself.
    """
    config.require("slack_channel_id")
    deadline = config.schedule["order_deadline"].time_str
    blocks = polls.build_order_reminder_blocks(deadline)
    client.chat_postMessage(
        channel=config.slack_channel_id,
        blocks=blocks,
        text=f"Reminder: place your lunch orders before {deadline}!",
    )
    logger.info("Posted order reminder (deadline %s)", deadline)


def build_scheduler(config: Config, conn, client):
    """Create a ``BackgroundScheduler`` with the three configured jobs registered.

    Builds cron triggers from ``config.schedule`` (poll_create / poll_close /
    order_reminder) using ``config.timezone``. ``order_deadline`` is
    informational only and is NOT scheduled. The caller is responsible for
    ``scheduler.start()`` and keeping the process alive (see :mod:`lunch_bot.main`).
    """
    from apscheduler.schedulers.background import BackgroundScheduler  # lazy
    from apscheduler.triggers.cron import CronTrigger  # lazy

    scheduler = BackgroundScheduler(timezone=config.timezone)

    jobs = (
        ("poll_create", lambda: create_weekly_poll(config, conn, client)),
        ("poll_close", lambda: close_poll_and_announce(config, conn, client)),
        ("order_reminder", lambda: send_order_reminder(config, conn, client)),
    )
    for job_id, func in jobs:
        entry = config.schedule[job_id]
        scheduler.add_job(
            func,
            CronTrigger(
                day_of_week=entry.day,
                hour=entry.hour,
                minute=entry.minute,
                timezone=config.timezone,
            ),
            id=job_id,
            replace_existing=True,
        )
    return scheduler
