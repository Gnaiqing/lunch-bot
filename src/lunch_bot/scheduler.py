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


def create_weekly_poll(
    config: Config,
    conn,
    client,
    *,
    poll_size: int | None = None,
    required_restaurant_ids: list[int] | None = None,
) -> int | None:
    """Select candidates and post the poll-create-day poll to the channel.

    Selection is intentionally limited to the current active candidate list.
    Poll creation never runs Google Places discovery or silently adds/reactivates
    restaurants. Returns the new poll id, or ``None`` if the list is empty.
    """
    config.require("slack_channel_id")

    active = db.get_active_restaurants(conn)
    if not active:
        logger.warning("No active restaurants in the pool; skipping poll creation.")
        return None

    requested_size = poll_size if poll_size is not None else config.poll_size
    requested_size = min(requested_size, config.max_poll_options)
    required_ids = list(dict.fromkeys(required_restaurant_ids or []))[
        : config.max_poll_options
    ]
    active_by_id = {restaurant.id: restaurant for restaurant in active}
    required = [active_by_id[rid] for rid in required_ids if rid in active_by_id]
    requested_size = max(requested_size, len(required))
    remaining = [restaurant for restaurant in active if restaurant.id not in required_ids]
    candidates = required + select_candidates(
        remaining,
        n=max(0, requested_size - len(required)),
        rng=random.Random(),
        exploration_c=config.exploration_c,
    )
    if not candidates:
        return None

    option_ids = [c.id for c in candidates if c.id is not None]

    # Poll closes on the configured poll_close day (Mon -> Wed by default).
    closes_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    # Create the poll row + options (needed to build the option action_ids) but do
    # NOT consume the selection yet: times_selected is incremented only once the
    # poll has actually been posted, so a failed Slack post leaves no trace.
    try:
        poll_id = db.create_poll(
            conn,
            config.slack_channel_id,
            option_ids,
            closes_at=closes_at,
            increment_selection=False,
        )
    except db.PollAlreadyOpenError:
        # The database constraint makes this check atomic with creation. In
        # particular, a second Slack request must not close or post over the poll
        # that won the race.
        logger.warning(
            "Skipped poll creation because channel %s already has an open poll.",
            config.slack_channel_id,
        )
        return None

    blocks = polls.build_poll_blocks(poll_id, candidates)
    try:
        resp = client.chat_postMessage(
            channel=config.slack_channel_id,
            blocks=blocks,
            text="This week's lunch poll is up!",
        )
    except Exception as exc:
        # Post failed: roll back the poll so no open poll with no Slack ts is left
        # behind (the close job would otherwise close an orphan), and do NOT
        # increment times_selected.
        db.delete_poll(conn, poll_id)
        logger.error("Failed to post weekly poll %s; rolled it back (%s).", poll_id, exc)
        return None

    ts = resp.get("ts")
    if ts:
        db.set_poll_ts(conn, poll_id, ts)
    # Only now that the poll is live do we consume the selection.
    db.increment_selection_counts(conn, option_ids)
    logger.info("Posted weekly poll %s with %d options", poll_id, len(candidates))
    return poll_id


def close_poll_and_announce(
    config: Config, conn, client, *, reading_group_time: str | None = None
) -> bool:
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
        return False
    poll_id = open_poll["id"]

    # Close + tally + winner selection happen in ONE atomic DB operation that
    # serialises with vote recording on the same lock, so no vote can interleave
    # between tallying and closing, and the (additive) totals fold is applied
    # exactly once. ``applied`` is False if the poll was already closed (e.g. a
    # concurrent/duplicate run won the race) — then there's nothing to announce.
    applied, winner_id = db.close_poll_and_tally(conn, poll_id)
    if not applied:
        logger.warning("Poll %s was already closed; skipping tally + announce.", poll_id)
        return False

    # Disable the original vote buttons and show the final tally. Failure to
    # refresh Slack must not reopen an already atomically closed poll.
    if open_poll["slack_ts"]:
        option_ids = db.get_poll_option_ids(conn, poll_id)
        restaurants = [db.get_restaurant(conn, restaurant_id) for restaurant_id in option_ids]
        restaurants = [restaurant for restaurant in restaurants if restaurant is not None]
        try:
            client.chat_update(
                channel=config.slack_channel_id,
                ts=open_poll["slack_ts"],
                blocks=polls.build_poll_blocks(
                    poll_id,
                    restaurants,
                    tally=db.tally_votes(conn, poll_id),
                    voters=db.get_poll_voters(conn, poll_id),
                    closed=True,
                ),
                text="Lunch poll closed",
            )
        except Exception as exc:  # pragma: no cover - Slack network best-effort
            logger.warning("Closed poll %s but could not refresh its Slack message: %s", poll_id, exc)

    if winner_id is None:
        client.chat_postMessage(
            channel=config.slack_channel_id,
            text="No votes were cast this week — no lunch winner. 😢",
        )
        return True

    winner = db.get_restaurant(conn, winner_id)
    summary = build_order_summary(winner, reading_group_time=reading_group_time)
    client.chat_postMessage(
        channel=config.slack_channel_id,
        blocks=summary["blocks"],
        text=summary["text"],
    )
    logger.info("Announced winner %s for poll %s", winner.name, poll_id)
    return True


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


def build_scheduler(config: Config, conn, client, *, llm=None):
    """Create a ``BackgroundScheduler`` with the three configured jobs registered.

    Builds cron triggers from ``config.schedule`` (poll_create / poll_close /
    order_reminder) using ``config.timezone``. ``order_deadline`` is
    informational only and is NOT scheduled. ``llm`` remains an accepted
    compatibility argument but poll creation does not perform discovery. The
    caller is responsible for ``scheduler.start()`` and keeping the process
    alive (see :mod:`lunch_bot.main`).
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
