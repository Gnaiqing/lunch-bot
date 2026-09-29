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
