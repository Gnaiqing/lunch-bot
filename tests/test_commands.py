"""Tests for deterministic conversational Slack command parsing."""

from lunch_bot.commands import match_restaurants, parse_mention_command
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


def test_list_current_restaurants():
    assert parse_mention_command("what are the current restaurants available?").kind == "list_restaurants"


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


def test_match_restaurant_name_before_cuisine():
    pool = [
        Restaurant(id=1, name="Pala 148", cuisine="Pizza"),
        Restaurant(id=2, name="8 Mile Pizza", cuisine="Pizza"),
    ]
    assert [r.id for r in match_restaurants(pool, "Pala 148")] == [1]
    assert {r.id for r in match_restaurants(pool, "pizza")} == {1, 2}
    assert match_restaurants(pool, "Pizza Pizza") == []
