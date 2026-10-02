"""Tests for the SQLite persistence + vote-guard logic.

These exercise pure DB helpers against a throwaway sqlite file (pytest's
``tmp_path``), so no network, Slack, Google, or Anthropic access is needed. They
cover the Copilot review fixes:

- F1: a write succeeds from a worker thread (shared connection is thread-safe).
- F2: an incoming row with a ``place_id`` enriches an already-seeded row (matched
  by normalized name) instead of inserting a duplicate.
- F3: ``polls.handle_vote`` rejects votes on a closed poll or for a restaurant
  that isn't one of the poll's options.
"""

import threading
import sqlite3

from lunch_bot import db, polls
from lunch_bot.models import Restaurant


# ---------------------------------------------------------------------------
# F2 — seed-row de-duplication by normalized name.
# ---------------------------------------------------------------------------
def test_upsert_enriches_seed_row_by_normalized_name(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    # Seed row: no place_id (as loaded from the seed CSV).
    sid = db.upsert_restaurant(conn, Restaurant(name="Megumi Mazesoba", cuisine="Japanese"))

    # Discovery/suggestion finds the same place — different case + extra spaces,
    # and now carrying a place_id + address.
    did = db.upsert_restaurant(
        conn,
        Restaurant(name="megumi  mazesoba", place_id="PLACE123", address="123 Main St", source="places"),
    )

    assert did == sid  # enriched in place, not a new row
    rows = conn.execute("SELECT * FROM restaurants").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["place_id"] == "PLACE123"
    assert row["address"] == "123 Main St"
    assert row["cuisine"] == "Japanese"  # preserved via COALESCE


def test_upsert_distinct_restaurants_stay_separate(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    a = db.upsert_restaurant(conn, Restaurant(name="Pai Thai", place_id="A"))
    # Same place_id -> same row.
    a2 = db.upsert_restaurant(conn, Restaurant(name="Pai Thai", place_id="A"))
    assert a2 == a
    # A different place with a different place_id and name -> new row.
    b = db.upsert_restaurant(conn, Restaurant(name="Other Spot", place_id="B"))
    assert b != a
    assert conn.execute("SELECT COUNT(*) AS c FROM restaurants").fetchone()["c"] == 2


def test_upsert_does_not_match_already_validated_rows(tmp_path):
    """The name fallback only enriches un-validated (NULL place_id) rows, so a
    second, genuinely different place with the same name is not merged."""
    conn = db.init_db(str(tmp_path / "lunch.db"))
    first = db.upsert_restaurant(conn, Restaurant(name="Twin Name", place_id="P1"))
    second = db.upsert_restaurant(conn, Restaurant(name="Twin Name", place_id="P2"))
    assert second != first
    assert conn.execute("SELECT COUNT(*) AS c FROM restaurants").fetchone()["c"] == 2


def test_soft_remove_and_reactivate_restaurant_preserves_row(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    restaurant_id = db.upsert_restaurant(
        conn, Restaurant(name="Not Actually a Restaurant", cuisine="other")
    )

    assert db.set_restaurants_active(conn, [restaurant_id], active=False) == 1
    assert db.get_active_restaurants(conn) == []
    assert db.get_restaurant(conn, restaurant_id).active is False

    assert db.set_restaurants_active(conn, [restaurant_id], active=True) == 1
    assert [restaurant.id for restaurant in db.get_active_restaurants(conn)] == [
        restaurant_id
    ]


def test_merge_restaurants_preserves_poll_votes_and_history(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    source = db.upsert_restaurant(conn, Restaurant(name="Raku duplicate"))
    destination = db.upsert_restaurant(conn, Restaurant(name="Raku"))
    poll_id = db.create_poll(conn, "C1", [source, destination])
    db.record_vote_if_open(conn, poll_id, source, "U1")
    db.record_vote_if_open(conn, poll_id, destination, "U1")
    db.record_vote_if_open(conn, poll_id, source, "U2")

    db.merge_restaurants(conn, source, destination)

    assert db.get_poll_option_ids(conn, poll_id) == [destination]
    assert db.tally_votes(conn, poll_id) == {destination: 2}
    assert db.get_restaurant(conn, source).active is False


def test_cancel_poll_does_not_fold_vote_totals(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, restaurant_id, _ = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, restaurant_id, "U1")

    assert db.cancel_poll_if_open(conn, poll_id) is True
    assert db.get_poll(conn, poll_id)["status"] == "cancelled"
    assert db.get_restaurant(conn, restaurant_id).total_votes == 0
    assert db.cancel_poll_if_open(conn, poll_id) is False


# ---------------------------------------------------------------------------
# F3 — vote guarding.
# ---------------------------------------------------------------------------
def _two_option_poll(conn):
    r1 = db.upsert_restaurant(conn, Restaurant(name="A", cuisine="x"))
    r2 = db.upsert_restaurant(conn, Restaurant(name="B", cuisine="y"))
    poll_id = db.create_poll(conn, "C1", [r1, r2])
    return poll_id, r1, r2


def test_handle_vote_records_valid_open_vote(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, _ = _two_option_poll(conn)
    result = polls.handle_vote(conn, polls.vote_action_id(poll_id, r1), "U1")
    assert result == (poll_id, r1)
    assert db.tally_votes(conn, poll_id) == {r1: 1}


def test_handle_vote_rejects_closed_poll(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    polls.handle_vote(conn, polls.vote_action_id(poll_id, r1), "U1")
    db.close_poll(conn, poll_id, r1)

    # Late vote on a now-closed poll is ignored and the tally is unchanged.
    assert polls.handle_vote(conn, polls.vote_action_id(poll_id, r2), "U2") is None
    assert db.tally_votes(conn, poll_id) == {r1: 1}


def test_handle_vote_rejects_invalid_option(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, _, _ = _two_option_poll(conn)
    outsider = db.upsert_restaurant(conn, Restaurant(name="C", cuisine="z"))

    # 'outsider' is not one of this poll's options -> ignored, no tally change.
    assert polls.handle_vote(conn, polls.vote_action_id(poll_id, outsider), "U1") is None
    assert db.tally_votes(conn, poll_id) == {}


def test_handle_vote_ignores_non_vote_action(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    assert polls.handle_vote(conn, "not-a-vote-action", "U1") is None


def test_one_user_can_vote_for_multiple_options_and_toggle_one_off(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)

    assert polls.handle_vote(conn, polls.vote_action_id(poll_id, r1), "U1") == (poll_id, r1)
    assert polls.handle_vote(conn, polls.vote_action_id(poll_id, r2), "U1") == (poll_id, r2)
    assert db.tally_votes(conn, poll_id) == {r1: 1, r2: 1}

    # Clicking the first option again removes only that selection.
    assert polls.handle_vote(conn, polls.vote_action_id(poll_id, r1), "U1") == (poll_id, r1)
    assert db.tally_votes(conn, poll_id) == {r2: 1}


def test_poll_voters_are_grouped_by_option(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.record_vote_if_open(conn, poll_id, r1, "U2")
    db.record_vote_if_open(conn, poll_id, r2, "U2")

    assert db.get_poll_voters(conn, poll_id) == {
        r1: ["U1", "U2"],
        r2: ["U2"],
    }


def test_init_db_migrates_legacy_single_choice_votes(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    legacy_schema = db.SCHEMA.replace(
        "UNIQUE (poll_id, restaurant_id, slack_user_id)",
        "UNIQUE (poll_id, slack_user_id)",
    )
    conn.executescript(legacy_schema)
    conn.execute(
        "INSERT INTO restaurants (name, source, active, times_selected, total_votes, created_at) "
        "VALUES ('A', 'seed', 1, 0, 0, 'now')"
    )
    conn.execute(
        "INSERT INTO restaurants (name, source, active, times_selected, total_votes, created_at) "
        "VALUES ('B', 'seed', 1, 0, 0, 'now')"
    )
    conn.execute(
        "INSERT INTO polls (slack_channel, created_at, status) VALUES ('C1', 'now', 'open')"
    )
    conn.execute("INSERT INTO poll_options (poll_id, restaurant_id) VALUES (1, 1)")
    conn.execute("INSERT INTO poll_options (poll_id, restaurant_id) VALUES (1, 2)")
    conn.execute(
        "INSERT INTO votes (poll_id, restaurant_id, slack_user_id, created_at) "
        "VALUES (1, 1, 'U1', 'now')"
    )
    conn.commit()
    conn.close()

    migrated = db.init_db(path)
    # Existing vote survives, and the same user can now select another option.
    assert db.tally_votes(migrated, 1) == {1: 1}
    assert db.record_vote_if_open(migrated, 1, 2, "U1") is True
    assert db.tally_votes(migrated, 1) == {1: 1, 2: 1}
    indexes = {
        row[1] for row in migrated.execute("PRAGMA index_list(votes)").fetchall()
    }
    assert "idx_votes_poll" in indexes


def test_init_db_adds_maps_url_to_legacy_tables(tmp_path):
    path = str(tmp_path / "legacy-location.db")
    conn = sqlite3.connect(path)
    legacy_schema = db.SCHEMA.replace("    maps_url         TEXT,\n", "").replace(
        "    maps_url       TEXT,\n", ""
    )
    conn.executescript(legacy_schema)
    conn.close()

    migrated = db.init_db(path)
    restaurant_columns = {
        row["name"] for row in migrated.execute("PRAGMA table_info('restaurants')")
    }
    pending_columns = {
        row["name"]
        for row in migrated.execute(
            "PRAGMA table_info('pending_restaurant_confirmations')"
        )
    }
    assert "maps_url" in restaurant_columns
    assert "maps_url" in pending_columns


def test_init_db_reconciles_legacy_duplicate_open_polls(tmp_path):
    path = str(tmp_path / "legacy-open-polls.db")
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO polls (slack_channel, created_at, status) VALUES ('C1', 'first', 'open')"
    )
    conn.execute(
        "INSERT INTO polls (slack_channel, created_at, status) VALUES ('C1', 'second', 'open')"
    )
    conn.commit()
    conn.close()

    migrated = db.init_db(path)

    rows = migrated.execute(
        "SELECT id, status FROM polls WHERE slack_channel = 'C1' ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [
        (1, "cancelled"),
        (2, "open"),
    ]
    indexes = {
        row["name"] for row in migrated.execute("PRAGMA index_list('polls')").fetchall()
    }
    assert "idx_polls_one_open_per_channel" in indexes


def test_add_poll_option_enforces_maximum_inside_write(tmp_path):
    conn = db.init_db(str(tmp_path / "max-options.db"))
    restaurant_ids = [
        db.upsert_restaurant(conn, Restaurant(name=f"Restaurant {index}"))
        for index in range(3)
    ]
    poll_id = db.create_poll(conn, "C1", restaurant_ids[:2])

    assert (
        db.add_poll_option_if_open(
            conn, poll_id, restaurant_ids[2], max_options=2
        )
        is False
    )
    assert db.get_poll_option_ids(conn, poll_id) == restaurant_ids[:2]


# ---------------------------------------------------------------------------
# G2 — atomic check-and-insert vote guard (record_vote_if_open).
# ---------------------------------------------------------------------------
def test_record_vote_if_open_records_open_vote(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, _ = _two_option_poll(conn)
    assert db.record_vote_if_open(conn, poll_id, r1, "U1") is True
    assert db.tally_votes(conn, poll_id) == {r1: 1}


def test_record_vote_if_open_rejects_closed_poll(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.close_poll(conn, poll_id, r1)

    # The conditional write must not insert once the poll is closed.
    assert db.record_vote_if_open(conn, poll_id, r2, "U2") is False
    assert db.tally_votes(conn, poll_id) == {r1: 1}


def test_record_vote_if_open_rejects_invalid_option(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, _, _ = _two_option_poll(conn)
    outsider = db.upsert_restaurant(conn, Restaurant(name="C", cuisine="z"))
    assert db.record_vote_if_open(conn, poll_id, outsider, "U1") is False
    assert db.tally_votes(conn, poll_id) == {}


# ---------------------------------------------------------------------------
# G3/G4 — atomic close + tally + winner (close_poll_and_tally), idempotent.
# ---------------------------------------------------------------------------
def _total_votes(conn, rid):
    return conn.execute(
        "SELECT total_votes FROM restaurants WHERE id = ?", (rid,)
    ).fetchone()["total_votes"]


def test_close_poll_and_tally_counts_votes_and_picks_winner(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    # Votes recorded BEFORE close must be counted.
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.record_vote_if_open(conn, poll_id, r1, "U2")
    db.record_vote_if_open(conn, poll_id, r2, "U3")

    applied, winner_id = db.close_poll_and_tally(conn, poll_id)
    assert applied is True
    assert winner_id == r1  # 2 votes beats 1
    assert db.get_poll(conn, poll_id)["status"] == "closed"
    assert db.get_poll(conn, poll_id)["winner_restaurant_id"] == r1
    # Totals folded into the pool exactly once.
    assert _total_votes(conn, r1) == 2
    assert _total_votes(conn, r2) == 1


def test_close_poll_and_tally_is_idempotent(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.record_vote_if_open(conn, poll_id, r1, "U2")
    db.record_vote_if_open(conn, poll_id, r2, "U3")

    first = db.close_poll_and_tally(conn, poll_id)
    assert first == (True, r1)

    # A re-run (e.g. after a crash between commit and close) must be a no-op and
    # must NOT double-apply the additive totals.
    second = db.close_poll_and_tally(conn, poll_id)
    assert second == (False, None)
    assert _total_votes(conn, r1) == 2
    assert _total_votes(conn, r2) == 1


def test_close_poll_and_tally_no_votes(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    applied, winner_id = db.close_poll_and_tally(conn, poll_id)
    assert applied is True
    assert winner_id is None
    assert db.get_poll(conn, poll_id)["status"] == "closed"
    assert _total_votes(conn, r1) == 0
    assert _total_votes(conn, r2) == 0


def test_close_poll_and_tally_tie_breaks_by_lowest_id(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.record_vote_if_open(conn, poll_id, r2, "U2")
    applied, winner_id = db.close_poll_and_tally(conn, poll_id)
    assert applied is True
    assert winner_id == min(r1, r2)


def test_vote_after_close_is_rejected(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    poll_id, r1, r2 = _two_option_poll(conn)
    db.record_vote_if_open(conn, poll_id, r1, "U1")
    db.close_poll_and_tally(conn, poll_id)

    # A late vote after the atomic close cannot land in the tally.
    assert db.record_vote_if_open(conn, poll_id, r2, "U2") is False
    assert db.tally_votes(conn, poll_id) == {r1: 1}


# ---------------------------------------------------------------------------
# F1 — cross-thread write smoke test.
# ---------------------------------------------------------------------------
def test_write_from_separate_thread(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    errors: list[Exception] = []

    def worker():
        try:
            db.upsert_restaurant(conn, Restaurant(name="Threaded", cuisine="x"))
        except Exception as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert errors == [], f"write from worker thread raised: {errors}"
    assert conn.execute("SELECT COUNT(*) AS c FROM restaurants").fetchone()["c"] == 1
