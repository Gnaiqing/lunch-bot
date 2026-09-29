"""Network-free integration tests for conversational Slack mention handling."""

from lunch_bot import db
from lunch_bot.config import load_config
from lunch_bot.models import Restaurant
from lunch_bot.slack_app import build_app


class _FakeBoltApp:
    def __init__(self, **_kwargs):
        self.events = {}
        self.actions = []

    def event(self, name):
        def register(func):
            self.events[name] = func
            return func

        return register

    def action(self, pattern):
        def register(func):
            self.actions.append((pattern, func))
            return func

        return register


class _FakeSlackClient:
    def __init__(self):
        self.posts = []
        self.updates = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1700000000.000001"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


def test_mentions_create_four_choice_poll_then_add_cuisine(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    for index in range(6):
        db.upsert_restaurant(
            conn, Restaurant(name=f"Candidate {index}", cuisine=f"cuisine-{index}")
        )

    app = build_app(config, conn)
    mention = app.events["app_mention"]
    client = _FakeSlackClient()
    replies = []

    mention(
        event={
            "channel": "C_TEST",
            "text": "<@U_BOT> can you create a polly for the next reading group with 4 choices?",
        },
        say=replies.append,
        client=client,
    )

    poll = db.get_open_poll(conn, "C_TEST")
    assert poll is not None
    assert len(db.get_poll_option_ids(conn, poll["id"])) == 4
    assert len(client.posts) == 1

    pizza_id = db.upsert_restaurant(conn, Restaurant(name="Pizza Place", cuisine="Pizza"))
    mention(
        event={
            "channel": "C_TEST",
            "text": "<@U_BOT> can we add a pizza restaurant in this week's polly?",
        },
        say=replies.append,
        client=client,
    )

    assert pizza_id in db.get_poll_option_ids(conn, poll["id"])
    assert len(client.updates) == 1
    assert any("Added to the current poll" in reply for reply in replies)
