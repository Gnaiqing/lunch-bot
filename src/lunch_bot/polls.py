"""Slack Block Kit poll construction, vote handling, and tallying.

The bot runs its OWN poll (not Polly) using Block Kit buttons, so votes are
captured directly and stored in SQLite. Each option is a button whose
``action_id`` encodes the poll id and restaurant id.

This module has no hard dependency on slack-bolt — it only builds and interprets
plain dict payloads (Block Kit JSON), so it can be unit-tested without Slack.
"""

from __future__ import annotations

from typing import Optional

from . import db
from .models import Restaurant

# action_id prefix for a vote button: "vote::<poll_id>::<restaurant_id>"
VOTE_ACTION_PREFIX = "vote"


def vote_action_id(poll_id: int, restaurant_id: int) -> str:
    """Encode a poll+restaurant into a Block Kit ``action_id``."""
    return f"{VOTE_ACTION_PREFIX}::{poll_id}::{restaurant_id}"


def parse_vote_action_id(action_id: str) -> Optional[tuple[int, int]]:
    """Decode a vote ``action_id`` back into ``(poll_id, restaurant_id)``.

    Returns ``None`` if the action_id is not a vote action.
    """
    parts = action_id.split("::")
    if len(parts) != 3 or parts[0] != VOTE_ACTION_PREFIX:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _restaurant_label(r: Restaurant) -> str:
    """Human label for a poll option button/line."""
    bits = [r.name]
    if r.cuisine:
        bits.append(f"({r.cuisine})")
    return " ".join(bits)


def build_poll_blocks(
    poll_id: int,
    restaurants: list[Restaurant],
    *,
    tally: Optional[dict[int, int]] = None,
    closed: bool = False,
    header: str = "🍽️ Lunch poll — vote for this week's pick!",
) -> list[dict]:
    """Build the Block Kit blocks for a poll message.

    Args:
        poll_id: The poll's DB id (encoded into button action_ids).
        restaurants: The options, in display order.
        tally: Optional ``{restaurant_id: votes}`` to render current counts.
        closed: If ``True``, render a closed/read-only view (no buttons).
        header: The header text.

    Returns:
        A list of Block Kit block dicts suitable for ``chat_postMessage``.
    """
    tally = tally or {}
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
    ]

    for r in restaurants:
        count = tally.get(r.id, 0)
        line = _restaurant_label(r)
        if tally or closed:
            line += f" — {count} vote" + ("" if count == 1 else "s")
        section = {"type": "section", "text": {"type": "mrkdwn", "text": f"*{line}*"}}
        if not closed and r.id is not None:
            section["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "Vote"},
                "action_id": vote_action_id(poll_id, r.id),
                "value": str(r.id),
            }
        blocks.append(section)

    if closed:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Poll closed."}]}
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "One vote per person — click again to change it."}
                ],
            }
        )
    return blocks


def handle_vote(conn, action_id: str, slack_user_id: str) -> Optional[tuple[int, int]]:
    """Record a vote from a button click.

    Returns ``(poll_id, restaurant_id)`` on success, or ``None`` if the action
    was not a recognisable vote action.
    """
    parsed = parse_vote_action_id(action_id)
    if not parsed:
        return None
    poll_id, restaurant_id = parsed
    db.record_vote(conn, poll_id, restaurant_id, slack_user_id)
    return poll_id, restaurant_id


def determine_winner(conn, poll_id: int) -> Optional[int]:
    """Return the winning restaurant id for a poll (highest tally).

    Ties are broken by lowest restaurant id for determinism. Returns ``None`` if
    no votes were cast.
    """
    tally = db.tally_votes(conn, poll_id)
    if not tally:
        return None
    # Sort by (-votes, restaurant_id) so the top vote-getter wins, ties -> lowest id.
    winner = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return winner
