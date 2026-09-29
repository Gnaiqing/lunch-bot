"""Tests for :mod:`lunch_bot.scheduler` poll-creation robustness.

Network-free: the Slack client is a stub and the DB is a throwaway sqlite file
(pytest's ``tmp_path``). These cover the round-3 review fixes:

- H2: if the Slack post fails, ``create_weekly_poll`` leaves no open poll behind
  and does NOT consume the selection (``times_selected`` stays put).
- H3: with a pre-existing open poll, ``create_weekly_poll`` reconciles it so at
  most one poll is open, and the normal path still works when none is open.
"""

from lunch_bot import db, scheduler
from lunch_bot.config import load_config
from lunch_bot.models import Restaurant


def _config(channel="C_TEST"):
    # No GOOGLE_MAPS_API_KEY -> discovery is skipped; small poll size so a handful
    # of seeded restaurants is enough.
    return load_config(
        env={"SLACK_CHANNEL_ID": channel, "POLL_SIZE": "3"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )


def _seed(conn, n=4):
    ids = []
    for i in range(n):
        ids.append(
            db.upsert_restaurant(
                conn, Restaurant(name=f"Resto {i}", cuisine="italian" if i % 2 else "thai")
            )
        )
    return ids


class _OkClient:
    """Slack client stub whose post succeeds and records the call."""

    def __init__(self):
        self.calls = 0

    def chat_postMessage(self, **kwargs):
        self.calls += 1
        return {"ok": True, "ts": f"170000000.{self.calls:06d}"}


class _FailingClient:
    """Slack client stub whose post always raises."""

    def __init__(self):
        self.calls = 0

    def chat_postMessage(self, **kwargs):
        self.calls += 1
        raise RuntimeError("slack is down")


def _open_polls(conn, channel="C_TEST"):
    return conn.execute(
        "SELECT * FROM polls WHERE slack_channel = ? AND status = 'open'", (channel,)
    ).fetchall()


def _times_selected(conn):
    return {
        r["id"]: r["times_selected"]
        for r in conn.execute("SELECT id, times_selected FROM restaurants").fetchall()
    }


# ---------------------------------------------------------------------------
# H2 — Slack post failure leaves no orphan poll and consumes no selection.
# ---------------------------------------------------------------------------
def test_post_failure_rolls_back_and_does_not_consume_selection(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    _seed(conn)
    before = _times_selected(conn)

    config = _config()
    client = _FailingClient()
    result = scheduler.create_weekly_poll(config, conn, client)

    assert result is None
    assert client.calls == 1  # it did attempt the post
    # No open (or any) poll left behind, and no options either.
    assert _open_polls(conn) == []
    assert conn.execute("SELECT COUNT(*) AS c FROM polls").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM poll_options").fetchone()["c"] == 0
    # times_selected untouched — the selection was never consumed.
    assert _times_selected(conn) == before


# ---------------------------------------------------------------------------
# H2 (happy path) — a successful post opens exactly one poll, stores the ts,
# and consumes the selection once.
# ---------------------------------------------------------------------------
def test_successful_post_opens_one_poll_and_consumes_selection(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    _seed(conn)
    before = _times_selected(conn)

    config = _config()
    client = _OkClient()
    poll_id = scheduler.create_weekly_poll(config, conn, client)

    assert poll_id is not None
    open_polls = _open_polls(conn)
    assert len(open_polls) == 1
    poll = db.get_poll(conn, poll_id)
    assert poll["slack_ts"] is not None  # ts stored on success

    # Exactly the offered options had their selection consumed once.
    option_ids = set(db.get_poll_option_ids(conn, poll_id))
    after = _times_selected(conn)
    for rid, count in after.items():
        expected = before[rid] + (1 if rid in option_ids else 0)
        assert count == expected


# ---------------------------------------------------------------------------
# H3 — a pre-existing open poll is reconciled so at most one poll stays open.
# ---------------------------------------------------------------------------
def test_preexisting_open_poll_is_reconciled(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    ids = _seed(conn)

    # Simulate a stale open poll from a prior week that never got closed.
    stale_id = db.create_poll(conn, "C_TEST", ids[:3], closes_at="2000-01-01T00:00:00+00:00")
    assert len(_open_polls(conn)) == 1

    config = _config()
    client = _OkClient()
    new_id = scheduler.create_weekly_poll(config, conn, client)

    assert new_id is not None and new_id != stale_id
    # At most one open poll — the new one; the stale poll is now closed.
    open_polls = _open_polls(conn)
    assert len(open_polls) == 1
    assert open_polls[0]["id"] == new_id
    assert db.get_poll(conn, stale_id)["status"] == "closed"
    # Reconciliation posts no winner announcement: only the new poll was posted.
    assert client.calls == 1
