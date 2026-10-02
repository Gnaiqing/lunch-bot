"""Tests for conversational Slack command parsing and route validation."""

import json

import pytest

from lunch_bot.commands import match_restaurants, parse_mention_command, parse_semantic_route
from lunch_bot.models import Restaurant


def test_create_polly_with_count():
    command = parse_mention_command("can you create a polly for next week with 4 choices?")
    assert command.kind == "create_poll"
    assert command.count == 4
    assert command.queries == []


def test_create_poll_with_required_restaurant():
    command = parse_mention_command("create a poll with 4 choices including Pala 148")
    assert command.kind == "create_poll"
    assert command.count == 4
    assert command.queries == ["Pala 148"]


def test_add_cuisine_to_current_polly():
    command = parse_mention_command("can we add a pizza restaurant in this week's polly?")
    assert command.kind == "add_to_poll"
    assert command.queries == ["pizza"]


def test_add_to_poll_handles_curly_apostrophe_and_open_poll_wording():
    command = parse_mention_command(
        "Can you add a pizza restaurant to this week’s polly?"
    )
    assert command.kind == "add_to_poll"
    assert command.queries == ["pizza"]

    command = parse_mention_command("add Pizza Rustica to the open poll")
    assert command.kind == "add_to_poll"
    assert command.queries == ["Pizza Rustica"]


def test_list_current_restaurants():
    assert parse_mention_command("what are the current restaurants available?").kind == "list_restaurants"


def test_restaurant_location_question_extracts_name():
    command = parse_mention_command(
        "where is Miznon? Can you provide its google map link?"
    )
    assert command.kind == "restaurant_location"
    assert command.queries == ["Miznon"]


def test_introduction_is_help_not_restaurant_mutation():
    command = parse_mention_command("can you introduce yourself and the service you can provide?")
    assert command.kind == "help"
    assert command.queries == []


def test_unknown_question_uses_conversation_mode():
    assert parse_mention_command("how is your day going?").kind == "conversation"


def test_bare_text_is_not_a_restaurant_suggestion():
    assert parse_mention_command("Pai Northern Thai").kind == "conversation"
    assert parse_mention_command("try Pai Northern Thai").kind == "conversation"


def test_explicit_restaurant_suggestion_is_mutating():
    command = parse_mention_command("restaurant suggestion: Pai Northern Thai")
    assert command.kind == "add_to_pool"
    assert command.queries == ["Pai Northern Thai"]


def test_add_to_candidate_list():
    command = parse_mention_command("add Pai Northern Thai to the candidate list")
    assert command.kind == "add_to_pool"
    assert command.queries == ["Pai Northern Thai"]

    command = parse_mention_command("add restaurant Raku to the list")
    assert command.kind == "add_to_pool"
    assert command.queries == ["Raku"]


def test_remove_from_poll_and_candidate_category():
    command = parse_mention_command("remove Miznon from the current poll")
    assert command.kind == "remove_from_poll"
    assert command.queries == ["Miznon"]

    command = parse_mention_command(
        "remove all items in other from the candidate list"
    )
    assert command.kind == "remove_from_pool"
    assert command.queries == []
    assert command.cuisines == ["other"]


def test_close_poll_and_close_then_create_poll():
    command = parse_mention_command("close the current poll")
    assert command.kind == "close_poll"

    command = parse_mention_command(
        "Close the current poll. Start a new poll with 4 choices"
    )
    assert command.kind == "close_and_create_poll"
    assert command.count == 4


def test_manual_order_reminder_command():
    command = parse_mention_command("send the order reminder")
    assert command.kind == "send_order_reminder"


def test_explore_nearby_restaurants_is_read_only():
    command = parse_mention_command("explore 7 nearby restaurants")
    assert command.kind == "explore_restaurants"
    assert command.count == 7

    routed = parse_semantic_route(
        _route(intent="explore_restaurants", mode="answer", count=10)
    )
    assert routed.kind == "explore_restaurants"
    assert routed.count == 10


def test_match_restaurant_name_before_cuisine():
    pool = [
        Restaurant(id=1, name="Pala 148", cuisine="Pizza"),
        Restaurant(id=2, name="8 Mile Pizza", cuisine="Pizza"),
    ]
    assert [r.id for r in match_restaurants(pool, "Pala 148")] == [1]
    assert {r.id for r in match_restaurants(pool, "pizza")} == {1, 2}
    assert match_restaurants(pool, "Pizza Pizza") == []


def _route(**overrides):
    value = {
        "intent": "conversation",
        "mode": "answer",
        "restaurant_names": [],
        "cuisines": [],
        "count": None,
        "negated": False,
        "hypothetical": False,
        "ambiguous": False,
        "clarification": None,
    }
    value.update(overrides)
    return json.dumps(value)


def test_semantic_route_preserves_full_name_and_cuisine_types():
    named = parse_semantic_route(
        _route(
            intent="add_to_poll",
            mode="execute",
            restaurant_names=["Za Cafe Pizzeria and Bar"],
        )
    )
    assert named.queries == ["Za Cafe Pizzeria and Bar"]
    assert named.cuisines == []

    cuisine = parse_semantic_route(
        _route(intent="add_to_poll", mode="execute", cuisines=["pizza"])
    )
    assert cuisine.queries == []
    assert cuisine.cuisines == ["pizza"]


def test_semantic_route_supports_scoped_removal():
    pool = parse_semantic_route(
        _route(
            intent="remove_from_pool",
            mode="execute",
            cuisines=["other"],
        )
    )
    assert pool.kind == "remove_from_pool"
    assert pool.cuisines == ["other"]

    poll = parse_semantic_route(
        _route(
            intent="remove_from_poll",
            mode="execute",
            restaurant_names=["Miznon"],
        )
    )
    assert poll.kind == "remove_from_poll"
    assert poll.queries == ["Miznon"]


def test_semantic_route_supports_manager_poll_close_actions():
    close = parse_semantic_route(
        _route(intent="close_poll", mode="execute")
    )
    assert close.kind == "close_poll"

    replace = parse_semantic_route(
        _route(intent="close_and_create_poll", mode="execute", count=4)
    )
    assert replace.kind == "close_and_create_poll"
    assert replace.count == 4


@pytest.mark.parametrize("flag", ["negated", "hypothetical"])
def test_semantic_route_never_executes_negated_or_hypothetical_action(flag):
    command = parse_semantic_route(
        _route(
            intent="add_to_poll",
            mode="execute",
            restaurant_names=["Miznon"],
            **{flag: True},
        )
    )
    assert command.kind == "conversation"


def test_semantic_route_turns_ambiguous_mutation_into_clarification():
    command = parse_semantic_route(
        _route(
            intent="add_to_poll",
            mode="execute",
            restaurant_names=["Cafe"],
            ambiguous=True,
            clarification="Which cafe do you mean?",
        )
    )
    assert command.kind == "clarify"
    assert command.clarification == "Which cafe do you mean?"


def test_semantic_route_rejects_malformed_or_incomplete_output():
    with pytest.raises(ValueError):
        parse_semantic_route("not json")
    with pytest.raises(ValueError):
        parse_semantic_route('{"intent": "create_poll"}')
