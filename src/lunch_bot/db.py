"""SQLite persistence layer using the stdlib ``sqlite3`` (no ORM).

Provides schema initialisation and small query helpers. A single database file
holds the restaurant pool, polls, poll options, and votes. The path is
configurable (see :class:`lunch_bot.config.Config`) and gitignored.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Restaurant

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    cuisine          TEXT,
    address          TEXT,
    place_id         TEXT UNIQUE,
    lat              REAL,
    lng              REAL,
    price_level      INTEGER,
    source           TEXT NOT NULL DEFAULT 'seed',
    active           INTEGER NOT NULL DEFAULT 1,
    times_selected   INTEGER NOT NULL DEFAULT 0,
    total_votes      INTEGER NOT NULL DEFAULT 0,
    last_selected_at TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slack_channel        TEXT NOT NULL,
    slack_ts             TEXT,
    created_at           TEXT NOT NULL,
    closes_at            TEXT,
    status               TEXT NOT NULL DEFAULT 'open',
    winner_restaurant_id INTEGER,
    FOREIGN KEY (winner_restaurant_id) REFERENCES restaurants(id)
);

CREATE TABLE IF NOT EXISTS poll_options (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id       INTEGER NOT NULL,
    restaurant_id INTEGER NOT NULL,
    FOREIGN KEY (poll_id) REFERENCES polls(id),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);

CREATE TABLE IF NOT EXISTS votes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id       INTEGER NOT NULL,
    restaurant_id INTEGER NOT NULL,
    slack_user_id TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (poll_id, slack_user_id),
    FOREIGN KEY (poll_id) REFERENCES polls(id),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);

CREATE INDEX IF NOT EXISTS idx_restaurants_active ON restaurants(active);
CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id);
CREATE INDEX IF NOT EXISTS idx_poll_options_poll ON poll_options(poll_id);
"""


def _now_iso() -> str:
    """UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """Open (and configure) a connection to the SQLite database.

    Rows are returned as ``sqlite3.Row`` for dict-like access, and foreign keys
    are enforced.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Create the schema if needed and return an open connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Restaurant helpers
# ---------------------------------------------------------------------------
def _row_to_restaurant(row: sqlite3.Row) -> Restaurant:
    return Restaurant(
        id=row["id"],
        name=row["name"],
        cuisine=row["cuisine"],
        address=row["address"],
        place_id=row["place_id"],
        lat=row["lat"],
        lng=row["lng"],
        price_level=row["price_level"],
        source=row["source"],
        active=bool(row["active"]),
        times_selected=row["times_selected"],
        total_votes=row["total_votes"],
        last_selected_at=row["last_selected_at"],
        created_at=row["created_at"],
    )


def upsert_restaurant(conn: sqlite3.Connection, r: Restaurant) -> int:
    """Insert a restaurant, or update it in place when the ``place_id`` matches.

    Restaurants without a ``place_id`` (e.g. seed rows not yet validated) are
    de-duplicated on a case-insensitive ``name`` match instead. Returns the row id.
    """
    created_at = r.created_at or _now_iso()

    if r.place_id:
        existing = conn.execute(
            "SELECT id FROM restaurants WHERE place_id = ?", (r.place_id,)
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM restaurants WHERE place_id IS NULL AND lower(name) = lower(?)",
            (r.name,),
        ).fetchone()

    if existing:
        rid = existing["id"]
        conn.execute(
            """
            UPDATE restaurants
               SET name = ?, cuisine = COALESCE(?, cuisine), address = COALESCE(?, address),
                   lat = COALESCE(?, lat), lng = COALESCE(?, lng),
                   price_level = COALESCE(?, price_level), active = ?
             WHERE id = ?
            """,
            (r.name, r.cuisine, r.address, r.lat, r.lng, r.price_level, int(r.active), rid),
        )
        conn.commit()
        return rid

    cur = conn.execute(
        """
        INSERT INTO restaurants
            (name, cuisine, address, place_id, lat, lng, price_level, source,
             active, times_selected, total_votes, last_selected_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r.name, r.cuisine, r.address, r.place_id, r.lat, r.lng, r.price_level,
            r.source, int(r.active), r.times_selected, r.total_votes,
            r.last_selected_at, created_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_active_restaurants(conn: sqlite3.Connection) -> list[Restaurant]:
    """Return all restaurants with ``active = 1``."""
    rows = conn.execute("SELECT * FROM restaurants WHERE active = 1").fetchall()
    return [_row_to_restaurant(r) for r in rows]


def get_restaurant(conn: sqlite3.Connection, restaurant_id: int) -> Optional[Restaurant]:
    row = conn.execute(
        "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
    ).fetchone()
    return _row_to_restaurant(row) if row else None


def restaurants_without_cuisine(conn: sqlite3.Connection) -> list[Restaurant]:
    """Return active restaurants whose cuisine has not been tagged yet."""
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE active = 1 AND (cuisine IS NULL OR cuisine = '')"
    ).fetchall()
    return [_row_to_restaurant(r) for r in rows]


def set_cuisine(conn: sqlite3.Connection, restaurant_id: int, cuisine: str) -> None:
    conn.execute(
        "UPDATE restaurants SET cuisine = ? WHERE id = ?", (cuisine, restaurant_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Poll helpers
# ---------------------------------------------------------------------------
def create_poll(
    conn: sqlite3.Connection,
    slack_channel: str,
    option_restaurant_ids: Iterable[int],
    *,
    closes_at: Optional[str] = None,
) -> int:
    """Create a poll and its options; return the poll id.

    Also increments ``times_selected`` and stamps ``last_selected_at`` on each
    offered restaurant so the selection algorithm's exploration term decays.
    """
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO polls (slack_channel, created_at, closes_at, status) VALUES (?, ?, ?, 'open')",
        (slack_channel, now, closes_at),
    )
    poll_id = int(cur.lastrowid)
    for rid in option_restaurant_ids:
        conn.execute(
            "INSERT INTO poll_options (poll_id, restaurant_id) VALUES (?, ?)",
            (poll_id, rid),
        )
        conn.execute(
            "UPDATE restaurants SET times_selected = times_selected + 1, last_selected_at = ? WHERE id = ?",
            (now, rid),
        )
    conn.commit()
    return poll_id


def set_poll_ts(conn: sqlite3.Connection, poll_id: int, slack_ts: str) -> None:
    """Store the Slack message timestamp for a poll (used to update the message)."""
    conn.execute("UPDATE polls SET slack_ts = ? WHERE id = ?", (slack_ts, poll_id))
    conn.commit()


def get_poll_option_ids(conn: sqlite3.Connection, poll_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT restaurant_id FROM poll_options WHERE poll_id = ?", (poll_id,)
    ).fetchall()
    return [r["restaurant_id"] for r in rows]


def get_open_poll(conn: sqlite3.Connection, slack_channel: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM polls WHERE slack_channel = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (slack_channel,),
    ).fetchone()


def close_poll(conn: sqlite3.Connection, poll_id: int, winner_restaurant_id: Optional[int]) -> None:
    conn.execute(
        "UPDATE polls SET status = 'closed', winner_restaurant_id = ? WHERE id = ?",
        (winner_restaurant_id, poll_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Vote helpers
# ---------------------------------------------------------------------------
def record_vote(
    conn: sqlite3.Connection, poll_id: int, restaurant_id: int, slack_user_id: str
) -> None:
    """Record (or change) a user's vote.

    The ``UNIQUE (poll_id, slack_user_id)`` constraint means a repeat vote from
    the same user updates their existing choice rather than adding a second one.
    """
    conn.execute(
        """
        INSERT INTO votes (poll_id, restaurant_id, slack_user_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (poll_id, slack_user_id)
        DO UPDATE SET restaurant_id = excluded.restaurant_id, created_at = excluded.created_at
        """,
        (poll_id, restaurant_id, slack_user_id, _now_iso()),
    )
    conn.commit()


def tally_votes(conn: sqlite3.Connection, poll_id: int) -> dict[int, int]:
    """Return ``{restaurant_id: vote_count}`` for a poll."""
    rows = conn.execute(
        "SELECT restaurant_id, COUNT(*) AS c FROM votes WHERE poll_id = ? GROUP BY restaurant_id",
        (poll_id,),
    ).fetchall()
    return {r["restaurant_id"]: r["c"] for r in rows}


def apply_vote_totals_to_pool(conn: sqlite3.Connection, poll_id: int) -> None:
    """Fold a closed poll's votes into each restaurant's ``total_votes``.

    Called when a poll closes so the selection algorithm can weight future picks
    by accumulated votes. Restaurants offered but receiving zero votes correctly
    gain nothing here (their ``times_selected`` already advanced at poll creation).
    """
    tally = tally_votes(conn, poll_id)
    for rid, count in tally.items():
        conn.execute(
            "UPDATE restaurants SET total_votes = total_votes + ? WHERE id = ?",
            (count, rid),
        )
    conn.commit()
