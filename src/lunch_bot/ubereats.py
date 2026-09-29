"""Uber Eats order-prep helpers (SEMI-AUTOMATED only).

IMPORTANT: This module intentionally does **not** create or place an Uber Eats
order. There is no official consumer group-order API, and v1 uses **no browser
automation**. The bot only builds a human-readable order summary and a search /
restaurant link and posts it to Slack; a human then places the actual order.

Everything here is pure string/dict building — no network, no automation.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .models import Restaurant

UBER_EATS_SEARCH_BASE = "https://www.ubereats.com/search"


def uber_eats_search_link(restaurant: Restaurant) -> str:
    """Build an Uber Eats search URL for the winning restaurant.

    We link to a *search* (not a specific store) because store URLs/IDs are not
    available without scraping. Including the address (when known) narrows the
    search to the right location.

    TODO(user): if you later capture a canonical Uber Eats store URL per
    restaurant, prefer linking straight to it here.
    """
    query = restaurant.name
    if restaurant.address:
        query = f"{restaurant.name} {restaurant.address}"
    return f"{UBER_EATS_SEARCH_BASE}?q={urllib.parse.quote(query)}"


def build_order_summary(
    restaurant: Restaurant,
    *,
    voters: Optional[list[str]] = None,
    reading_group_time: Optional[str] = None,
) -> dict:
    """Build the order-prep payload for Slack.

    Args:
        restaurant: The winning restaurant.
        voters: Optional list of Slack user ids/names who voted for the winner —
            handy for pinging the people who'll be eating.
        reading_group_time: Optional human string for when the food is needed.

    Returns:
        A dict with ``text`` (plain fallback) and ``blocks`` (Block Kit) plus the
        ``link`` for convenience/testing.
    """
    link = uber_eats_search_link(restaurant)
    voters = voters or []

    lines = [
        f"🏆 *This week's lunch winner: {restaurant.name}*",
        f"Cuisine: {restaurant.cuisine or 'n/a'}",
    ]
    if restaurant.address:
        lines.append(f"Address: {restaurant.address}")
    if reading_group_time:
        lines.append(f"Needed by: {reading_group_time}")
    lines.append(f"Suggested Uber Eats search (to save a lookup): {link}")
    lines.append(
        "*Organizer:* please create the Uber Eats *group order* and post the "
        "shareable link in this channel. The bot never creates or places the order."
    )
    if voters:
        lines.append("Voters: " + ", ".join(voters))

    text = "\n".join(lines)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🏆 Winner: {restaurant.name}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Uber Eats"},
                    "url": link,
                    "action_id": "open_uber_eats",
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "The bot never creates or places the order — the organizer sets up the group order and checks out.",
                }
            ],
        },
    ]
    return {"text": text, "blocks": blocks, "link": link}
