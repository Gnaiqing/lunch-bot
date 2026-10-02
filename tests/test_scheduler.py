"""Tests for :mod:`lunch_bot.scheduler` poll-creation robustness.

Network-free: the Slack client is a stub and the DB is a throwaway sqlite file
(pytest's ``tmp_path``). These cover the round-3 review fixes:

- H2: if the Slack post fails, ``create_weekly_poll`` leaves no open poll behind
  and does NOT consume the selection (``times_selected`` stays put).
- H3: with a pre-existing open poll, ``create_weekly_poll`` preserves it and the
  database invariant prevents a competing replacement from being posted.
"""

import threading

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


class _BlockingCreateClient(_OkClient):
    def __init__(self):
        super().__init__()
        self.post_started = threading.Event()
        self.release_post = threading.Event()
        self.updates = []

    def chat_postMessage(self, **kwargs):
        if kwargs["text"] == "This week's lunch poll is up!":
            self.post_started.set()
            assert self.release_post.wait(timeout=5)
        return super().chat_postMessage(**kwargs)

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)


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


def test_poll_cannot_be_closed_before_creation_post_and_timestamp_finish(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    _seed(conn)
    config = _config()
    client = _BlockingCreateClient()
    created_ids = []
    create_thread = threading.Thread(
        target=lambda: created_ids.append(
            scheduler.create_weekly_poll(config, conn, client)
        )
    )
    create_thread.start()
    assert client.post_started.wait(timeout=5)

    close_started = threading.Event()
    close_finished = threading.Event()

    def close_poll():
        close_started.set()
        scheduler.close_poll_and_announce(config, conn, client)
        close_finished.set()

    close_thread = threading.Thread(target=close_poll)
    close_thread.start()
    assert close_started.wait(timeout=5)
    assert not close_finished.wait(timeout=0.1)
    client.release_post.set()
    create_thread.join(timeout=5)
    close_thread.join(timeout=5)

    assert not create_thread.is_alive()
    assert not close_thread.is_alive()
    poll_id = created_ids[0]
    poll = db.get_poll(conn, poll_id)
    assert poll["status"] == "closed"
    assert poll["slack_ts"] is not None
    assert client.updates[-1]["text"] == "Lunch poll closed"
    assert all(
        "accessory" not in block
        for block in client.updates[-1]["blocks"]
        if block["type"] == "section"
    )


# ---------------------------------------------------------------------------
# H3 — a pre-existing open poll wins; a competing request changes nothing.
# ---------------------------------------------------------------------------
def test_preexisting_open_poll_is_preserved(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    ids = _seed(conn)

    # Simulate a stale open poll from a prior week that never got closed.
    stale_id = db.create_poll(conn, "C_TEST", ids[:3], closes_at="2000-01-01T00:00:00+00:00")
    assert len(_open_polls(conn)) == 1

    config = _config()
    client = _OkClient()
    new_id = scheduler.create_weekly_poll(config, conn, client)

    assert new_id is None
    open_polls = _open_polls(conn)
    assert len(open_polls) == 1
    assert open_polls[0]["id"] == stale_id
    assert db.get_poll(conn, stale_id)["status"] == "open"
    assert client.calls == 0


# ---------------------------------------------------------------------------
# H3 (database) — the partial unique index rejects a second open poll.
# ---------------------------------------------------------------------------
def test_database_rejects_second_open_poll_for_channel(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    ids = _seed(conn)
    first_id = db.create_poll(conn, "C_TEST", ids[:3])

    try:
        db.create_poll(conn, "C_TEST", ids[1:])
    except db.PollAlreadyOpenError:
        pass
    else:  # pragma: no cover - makes the expected constraint explicit
        raise AssertionError("a second open poll was accepted")

    assert [poll["id"] for poll in _open_polls(conn)] == [first_id]


def test_manual_poll_size_and_required_choice(tmp_path):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    ids = _seed(conn, n=6)
    config = _config()
    client = _OkClient()

    poll_id = scheduler.create_weekly_poll(
        config,
        conn,
        client,
        poll_size=4,
        required_restaurant_ids=[ids[-1]],
    )

    option_ids = db.get_poll_option_ids(conn, poll_id)
    assert len(option_ids) == 4
    assert ids[-1] in option_ids


def test_poll_creation_never_discovers_or_reactivates_removed_candidates(
    monkeypatch, tmp_path
):
    conn = db.init_db(str(tmp_path / "lunch.db"))
    active_ids = _seed(conn, n=4)
    removed_id = db.upsert_restaurant(
        conn,
        Restaurant(
            name="InterContinental Toronto Centre by IHG",
            cuisine="other",
            place_id="hotel-place",
            active=False,
        ),
    )
    config = load_config(
        env={
            "SLACK_CHANNEL_ID": "C_TEST",
            "GOOGLE_MAPS_API_KEY": "configured-but-must-not-be-used",
            "POLL_SIZE": "4",
        },
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    monkeypatch.setattr(
        "lunch_bot.discovery.discover_and_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("poll creation must not run discovery")
        ),
    )

    poll_id = scheduler.create_weekly_poll(config, conn, _OkClient())

    assert set(db.get_poll_option_ids(conn, poll_id)) == set(active_ids)
    assert removed_id not in db.get_poll_option_ids(conn, poll_id)
    assert db.get_restaurant(conn, removed_id).active is False
