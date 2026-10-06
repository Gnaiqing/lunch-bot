"""Plain dataclasses for the core domain objects.

These are deliberately dependency-free so that pure-logic modules (notably
``selection``) and their unit tests can import them without pulling in Slack,
Google, or Anthropic clients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Restaurant:
    """A candidate restaurant in the pool.

    Mirrors a row of the ``restaurants`` table. ``id`` and the various counters
    are optional so callers can build in-memory instances (e.g. in tests) before
    anything is persisted.
    """

    name: str
    cuisine: Optional[str] = None
    address: Optional[str] = None
    place_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    maps_url: Optional[str] = None
    price_level: Optional[int] = None
    source: str = "seed"  # one of: 'seed' | 'places' | 'suggestion'
    active: bool = True
    times_selected: int = 0
    total_votes: int = 0
    last_selected_at: Optional[str] = None  # ISO 8601 string
    created_at: Optional[str] = None  # ISO 8601 string
    id: Optional[int] = None


@dataclass
class Poll:
    """A weekly poll posted to Slack.

    Mirrors a row of the ``polls`` table.
    """

    slack_channel: str
    slack_ts: Optional[str] = None
    created_at: Optional[str] = None  # ISO 8601 string
    closes_at: Optional[str] = None  # ISO 8601 string
    status: str = "open"  # 'open' | 'closed'
    winner_restaurant_id: Optional[int] = None
    id: Optional[int] = None
    # Restaurant ids offered as options for this poll (not persisted here).
    option_restaurant_ids: list[int] = field(default_factory=list)


@dataclass
class Vote:
    """A single user's vote in a poll.

    Mirrors a row of the ``votes`` table. Uniqueness on
    ``(poll_id, restaurant_id, slack_user_id)`` is enforced at the DB layer so a
    user can select multiple options but only vote once for any one option.
    """

    poll_id: int
    restaurant_id: int
    slack_user_id: str
    created_at: Optional[str] = None  # ISO 8601 string
    id: Optional[int] = None
