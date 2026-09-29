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
    llm=None,
    poll_size: int | None = None,
    required_restaurant_ids: list[int] | None = None,
) -> int | None:
    """Select candidates and post the poll-create-day poll to the channel.

    Runs a Google Places discovery pass first (when a Maps API key is
    configured) so the pool is refreshed before candidates are chosen — a fresh,
    empty DB would otherwise have nothing to offer. Discovery is best-effort: if
    no key is set, or the API call fails, it logs and continues with the existing
    pool. Returns the new poll id, or ``None`` if there was nothing to offer.
    """
    config.require("slack_channel_id")

    if config.google_maps_api_key:
        try:
            from .discovery import discover_and_store  # lazy: pulls in googlemaps

            added = discover_and_store(conn, config, llm=llm)
            logger.info("Discovery pass touched %d restaurants before poll creation", added)
        except Exception as exc:
            logger.warning("Discovery failed (%s); continuing with existing pool.", exc)
    else:
        logger.info("No Google Maps API key configured; skipping discovery, using existing pool.")

    active = db.get_active_restaurants(conn)
    if not active:
        logger.warning("No active restaurants in the pool; skipping poll creation.")
        return None

    requested_size = poll_size if poll_size is not None else config.poll_size
    required_ids = list(dict.fromkeys(required_restaurant_ids or []))
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

    # Reconcile EVERY pre-existing open poll before opening a new one, so there is
    # never more than one open poll. A missed/failed close job leaves a stale poll,
    # and multiple can accumulate (legacy state, a prior version, or manual DB
    # recovery); get_open_poll() only ever revisits the newest, so we must close
    # them all here. Close + tally each in place WITHOUT announcing a winner — we
    # don't want a surprise message — then log that it was reconciled.
    for stale in db.get_open_polls(conn, config.slack_channel_id):
        db.close_poll_and_tally(conn, stale["id"])
        logger.warning(
            "Reconciled stale open poll %s (closed without announcement) before creating a new poll.",
            stale["id"],
        )

    option_ids = [c.id for c in candidates if c.id is not None]

    # Poll closes on the configured poll_close day (Mon -> Wed by default).
    closes_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    # Create the poll row + options (needed to build the option action_ids) but do
    # NOT consume the selection yet: times_selected is incremented only once the
    # poll has actually been posted, so a failed Slack post leaves no trace.
    poll_id = db.create_poll(
        conn,
        config.slack_channel_id,
        option_ids,
        closes_at=closes_at,
        increment_selection=False,
    )

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

    # Close + tally + winner selection happen in ONE atomic DB operation that
    # serialises with vote recording on the same lock, so no vote can interleave
    # between tallying and closing, and the (additive) totals fold is applied
    # exactly once. ``applied`` is False if the poll was already closed (e.g. a
    # concurrent/duplicate run won the race) — then there's nothing to announce.
    applied, winner_id = db.close_poll_and_tally(conn, poll_id)
    if not applied:
        logger.warning("Poll %s was already closed; skipping tally + announce.", poll_id)
        return

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


def build_scheduler(config: Config, conn, client, *, llm=None):
    """Create a ``BackgroundScheduler`` with the three configured jobs registered.

    Builds cron triggers from ``config.schedule`` (poll_create / poll_close /
    order_reminder) using ``config.timezone``. ``order_deadline`` is
    informational only and is NOT scheduled. ``llm`` (optional) is passed to the
    poll-create job so its discovery pass can tag cuisines. The caller is
    responsible for ``scheduler.start()`` and keeping the process alive (see
    :mod:`lunch_bot.main`).
    """
    from apscheduler.schedulers.background import BackgroundScheduler  # lazy
    from apscheduler.triggers.cron import CronTrigger  # lazy

    scheduler = BackgroundScheduler(timezone=config.timezone)

    jobs = (
        ("poll_create", lambda: create_weekly_poll(config, conn, client, llm=llm)),
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
