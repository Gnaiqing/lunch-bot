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
        self.ephemeral = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1700000000.000001"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)
        return {"ok": True}


class _FakeLLM:
    def answer_question(self, text, context):
        assert text == "why do you run lunch polls?"
        assert "Candidate restaurants" in context
        return "I help the reading group choose lunch."


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


def test_conversation_is_read_only_and_does_not_use_suggestion_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    db.upsert_restaurant(conn, Restaurant(name="Pala 148", cuisine="Pizza"))
    app = build_app(config, conn, llm=_FakeLLM())
    mention = app.events["app_mention"]
    replies = []
    before = len(db.get_active_restaurants(conn))

    mention(
        event={"channel": "C_TEST", "text": "<@U_BOT> why do you run lunch polls?"},
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert replies == ["I help the reading group choose lunch."]
    assert len(db.get_active_restaurants(conn)) == before


def test_new_restaurant_is_only_added_after_requester_confirms(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    monkeypatch.setattr(
        "lunch_bot.discovery.validate_suggestion",
        lambda _config, _name, _location=None: Restaurant(
            name="Pai Northern Thai",
            address="18 Duncan St, Toronto",
            place_id="pai-place",
            lat=43.6487,
            lng=-79.3888,
            price_level=2,
            source="suggestion",
        ),
    )
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    app = build_app(config, conn)
    client = _FakeSlackClient()
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> add Pai Northern Thai to the candidate list",
        },
        say=replies.append,
        client=client,
    )

    assert db.get_active_restaurants(conn) == []
    assert len(client.posts) == 1
    confirm_button = client.posts[0]["blocks"][2]["elements"][0]
    token = confirm_button["value"]
    pending = db.get_pending_restaurant_confirmation(conn, token)
    assert pending["status"] == "pending"
    assert pending["name"] == "Pai Northern Thai"

    actions = {pattern: handler for pattern, handler in app.actions if isinstance(pattern, str)}
    acknowledgements = []
    actions["restaurant_confirm"](
        ack=lambda: acknowledgements.append(True),
        body={
            "actions": [{"value": token}],
            "user": {"id": "U_REQUESTER"},
            "channel": {"id": "C_TEST"},
            "container": {"message_ts": "1700000000.000001"},
        },
        client=client,
    )

    assert acknowledgements == [True]
    restaurants = db.get_active_restaurants(conn)
    assert [restaurant.name for restaurant in restaurants] == ["Pai Northern Thai"]
    assert db.get_pending_restaurant_confirmation(conn, token)["status"] == "confirmed"


def test_different_user_cannot_confirm_restaurant(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    monkeypatch.setattr(
        "lunch_bot.discovery.validate_suggestion",
        lambda _config, _name, _location=None: Restaurant(
            name="Raku", place_id="raku-place", lat=43.65, lng=-79.39
        ),
    )
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    app = build_app(config, conn)
    client = _FakeSlackClient()
    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> restaurant suggestion: Raku",
        },
        say=lambda _message: None,
        client=client,
    )
    token = client.posts[0]["blocks"][2]["elements"][0]["value"]
    actions = {pattern: handler for pattern, handler in app.actions if isinstance(pattern, str)}

    actions["restaurant_confirm"](
        ack=lambda: None,
        body={
            "actions": [{"value": token}],
            "user": {"id": "U_OTHER"},
            "channel": {"id": "C_TEST"},
            "container": {"message_ts": "1700000000.000001"},
        },
        client=client,
    )

    assert db.get_active_restaurants(conn) == []
    assert db.get_pending_restaurant_confirmation(conn, token)["status"] == "pending"
    assert "Only the person" in client.ephemeral[0]["text"]


def test_new_poll_choice_waits_for_confirmation_before_database_and_poll(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    monkeypatch.setattr(
        "lunch_bot.discovery.validate_suggestion",
        lambda _config, _name, _location=None: Restaurant(
            name="New Lunch Spot",
            address="100 College St, Toronto",
            place_id="new-lunch-place",
            lat=43.66,
            lng=-79.39,
            price_level=1,
        ),
    )
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    existing_ids = [
        db.upsert_restaurant(conn, Restaurant(name=f"Existing {index}", cuisine="Other"))
        for index in range(2)
    ]
    poll_id = db.create_poll(conn, "C_TEST", existing_ids)
    db.set_poll_ts(conn, poll_id, "1700000000.000010")
    app = build_app(config, conn)
    client = _FakeSlackClient()
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> add New Lunch Spot to this week's poll",
        },
        say=replies.append,
        client=client,
    )

    assert len(db.get_active_restaurants(conn)) == 2
    assert db.get_poll_option_ids(conn, poll_id) == existing_ids
    token = client.posts[0]["blocks"][2]["elements"][0]["value"]
    actions = {pattern: handler for pattern, handler in app.actions if isinstance(pattern, str)}
    actions["restaurant_confirm"](
        ack=lambda: None,
        body={
            "actions": [{"value": token}],
            "user": {"id": "U_REQUESTER"},
            "channel": {"id": "C_TEST"},
            "container": {"message_ts": "1700000000.000020"},
        },
        client=client,
    )

    restaurants = db.get_active_restaurants(conn)
    added = next(restaurant for restaurant in restaurants if restaurant.name == "New Lunch Spot")
    assert added.id in db.get_poll_option_ids(conn, poll_id)
    assert db.get_pending_restaurant_confirmation(conn, token)["status"] == "confirmed"
