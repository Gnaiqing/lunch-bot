"""Network-free integration tests for conversational Slack mention handling."""

from lunch_bot import db
from lunch_bot.commands import MentionCommand, parse_mention_command
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


class _RoutingLLM:
    def route_message(self, text, _context):
        command = parse_mention_command(text)
        if command.kind == "add_to_poll" and command.queries == ["pizza"]:
            return MentionCommand("add_to_poll", cuisines=["pizza"])
        return command

    def parse_suggestion(self, text):
        return {"name": text, "location_hint": None}

    def answer_question(self, _text, _context):
        return "I can help with lunch polls."


class _FakeLLM(_RoutingLLM):
    def answer_question(self, text, context):
        assert text == "why do you run lunch polls?"
        assert "Candidate restaurants" in context
        return "I help the reading group choose lunch."


class _FixedRouter(_RoutingLLM):
    def __init__(self, command):
        self.command = command

    def route_message(self, _text, _context):
        return self.command


class _BrokenRouter(_RoutingLLM):
    def route_message(self, _text, _context):
        raise ValueError("invalid model output")


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

    app = build_app(config, conn, llm=_RoutingLLM())
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


def test_location_question_returns_stored_address_and_maps_link(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    db.upsert_restaurant(
        conn,
        Restaurant(
            name="Miznon",
            cuisine="Mediterranean",
            address="1235 Bay St., Toronto",
            place_id="MIZNON_PLACE",
            maps_url="https://maps.example/miznon",
        ),
    )
    app = build_app(config, conn, llm=_RoutingLLM())
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> where is Miznon? Can you provide its google map link?",
        },
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert "1235 Bay St., Toronto" in replies[0]
    assert "<https://maps.example/miznon|Open in Google Maps>" in replies[0]


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
    app = build_app(config, conn, llm=_RoutingLLM())
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
    app = build_app(config, conn, llm=_RoutingLLM())
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
    app = build_app(config, conn, llm=_RoutingLLM())
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


def test_add_existing_choice_preserves_poll_options_votes_and_message(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    original_ids = [
        db.upsert_restaurant(
            conn, Restaurant(name=f"Original {index}", cuisine=f"Cuisine {index}")
        )
        for index in range(4)
    ]
    pizza_id = db.upsert_restaurant(
        conn, Restaurant(name="Pizza Rustica", cuisine="Pizza")
    )
    poll_id = db.create_poll(conn, "C_TEST", original_ids)
    original_ts = "1700000000.000100"
    db.set_poll_ts(conn, poll_id, original_ts)
    db.record_vote_if_open(conn, poll_id, original_ids[0], "U_VOTER")
    app = build_app(config, conn, llm=_RoutingLLM())
    client = _FakeSlackClient()
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> add Pizza Rustica to the open poll",
        },
        say=replies.append,
        client=client,
    )

    open_poll = db.get_open_poll(conn, "C_TEST")
    assert open_poll["id"] == poll_id
    assert open_poll["slack_ts"] == original_ts
    assert db.get_poll_option_ids(conn, poll_id) == original_ids + [pizza_id]
    assert db.tally_votes(conn, poll_id) == {original_ids[0]: 1}
    assert db.get_poll_voters(conn, poll_id) == {original_ids[0]: ["U_VOTER"]}
    assert client.posts == []
    assert len(client.updates) == 1
    assert client.updates[0]["ts"] == original_ts
    assert "Added to the current poll" in replies[0]


def test_manual_create_refuses_to_replace_open_poll(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    option_ids = [
        db.upsert_restaurant(conn, Restaurant(name=f"Choice {index}"))
        for index in range(4)
    ]
    poll_id = db.create_poll(conn, "C_TEST", option_ids)
    db.set_poll_ts(conn, poll_id, "1700000000.000200")
    db.record_vote_if_open(conn, poll_id, option_ids[0], "U_VOTER")
    app = build_app(config, conn, llm=_RoutingLLM())
    client = _FakeSlackClient()
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> create a poll with 4 options",
        },
        say=replies.append,
        client=client,
    )

    assert db.get_open_poll(conn, "C_TEST")["id"] == poll_id
    assert db.get_poll_option_ids(conn, poll_id) == option_ids
    assert db.tally_votes(conn, poll_id) == {option_ids[0]: 1}
    assert client.posts == []
    assert "left it and its votes unchanged" in replies[0]


def test_semantic_router_understands_put_as_add_to_poll(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    existing_id = db.upsert_restaurant(conn, Restaurant(name="Existing"))
    miznon_id = db.upsert_restaurant(conn, Restaurant(name="Miznon", cuisine="Mediterranean"))
    poll_id = db.create_poll(conn, "C_TEST", [existing_id])
    db.set_poll_ts(conn, poll_id, "1700000000.000300")
    app = build_app(
        config,
        conn,
        llm=_FixedRouter(MentionCommand("add_to_poll", queries=["Miznon"])),
    )
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> put Miznon on this week's poll",
        },
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert db.get_poll_option_ids(conn, poll_id) == [existing_id, miznon_id]
    assert "Added to the current poll" in replies[0]


def test_router_failure_is_non_mutating(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    restaurant_id = db.upsert_restaurant(conn, Restaurant(name="Miznon"))
    poll_id = db.create_poll(conn, "C_TEST", [restaurant_id])
    db.set_poll_ts(conn, poll_id, "1700000000.000301")
    app = build_app(config, conn, llm=_BrokenRouter())
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> add Pizza Rustica to the poll",
        },
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert db.get_poll_option_ids(conn, poll_id) == [restaurant_id]
    assert "did not change anything" in replies[0]


def test_missing_router_refuses_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    for index in range(4):
        db.upsert_restaurant(conn, Restaurant(name=f"Candidate {index}"))
    app = build_app(config, conn)
    replies = []

    app.events["app_mention"](
        event={
            "channel": "C_TEST",
            "user": "U_REQUESTER",
            "text": "<@U_BOT> create a poll with 4 choices",
        },
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert db.get_open_poll(conn, "C_TEST") is None
    assert "did not change anything" in replies[0]


def test_ambiguous_restaurant_name_requests_full_name(monkeypatch, tmp_path):
    monkeypatch.setattr("slack_bolt.App", _FakeBoltApp)
    config = load_config(
        env={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C_TEST"},
        load_dotenv=False,
        config_path="__none__.yaml",
    )
    conn = db.init_db(str(tmp_path / "lunch.db"))
    base_id = db.upsert_restaurant(conn, Restaurant(name="Existing"))
    db.upsert_restaurant(conn, Restaurant(name="7 West Cafe", cuisine="Cafe"))
    db.upsert_restaurant(conn, Restaurant(name="Light Cafe", cuisine="Cafe"))
    poll_id = db.create_poll(conn, "C_TEST", [base_id])
    db.set_poll_ts(conn, poll_id, "1700000000.000302")
    app = build_app(
        config,
        conn,
        llm=_FixedRouter(MentionCommand("add_to_poll", queries=["Cafe"])),
    )
    replies = []

    app.events["app_mention"](
        event={"channel": "C_TEST", "user": "U_REQUESTER", "text": "<@U_BOT> add Cafe"},
        say=replies.append,
        client=_FakeSlackClient(),
    )

    assert db.get_poll_option_ids(conn, poll_id) == [base_id]
    assert "matches multiple restaurants" in replies[0]
