"""Parse conversational Slack mentions into safe lunch-bot commands.

The common commands are deliberately deterministic: creating or modifying a
poll must not depend on an LLM hallucinating an action. The parser accepts both
``poll`` and the commonly-used ``polly`` spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Restaurant


@dataclass(frozen=True)
class MentionCommand:
    kind: str
    count: int | None = None
    queries: list[str] = field(default_factory=list)


_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_POLL = r"poll(?:y)?"


def _clean_query(value: str) -> str:
    value = value.strip(" \t\n.,!?;:")
    value = re.sub(r"^(?:a|an|some|the)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+(?:restaurant|restaurants|choice|choices|option|options)$", "", value, flags=re.I)
    return value.strip()


def _split_queries(value: str) -> list[str]:
    value = re.sub(r"^(?:choices?|options?)\s+(?:of\s+)?", "", value, flags=re.I)
    parts = re.split(r"\s*,\s*|\s+and\s+", value)
    return [query for part in parts if (query := _clean_query(part))]


def _extract_count(text: str) -> int | None:
    match = re.search(r"\b(\d+)\s*(?:choices?|options?|restaurants?)\b", text, re.I)
    if match:
        return int(match.group(1))
    match = re.search(
        r"\b(" + "|".join(_NUMBER_WORDS) + r")\s+(?:choices?|options?|restaurants?)\b",
        text,
        re.I,
    )
    return _NUMBER_WORDS[match.group(1).lower()] if match else None


def parse_mention_command(text: str) -> MentionCommand:
    """Classify a mention and extract its count/restaurant or cuisine queries."""
    text = " ".join((text or "").strip().split())
    lowered = text.casefold()

    if not text or re.search(r"\b(help|commands?)\b", lowered) or "what can you do" in lowered:
        return MentionCommand("help")

    if re.search(r"\b(list|show|what|which)\b.*\b(restaurants?|candidates?|pool)\b", lowered) and not re.search(
        _POLL, lowered
    ):
        return MentionCommand("list_restaurants")

    if re.search(r"\b(list|show|what|which)\b.*\b(?:current|this week(?:'s)?)?\s*" + _POLL + r"\b", lowered) or re.search(
        r"\b(?:current|this week(?:'s)?)\s+" + _POLL + r"\s+(?:choices?|options?)\b", lowered
    ):
        return MentionCommand("list_poll")

    if re.search(r"\b(?:create|make|start|post|open)\b.*\b" + _POLL + r"\b", lowered):
        count = _extract_count(text)
        queries: list[str] = []
        # Named inclusions are introduced explicitly. A plain "with 4 choices"
        # is a count request, not a restaurant called "4".
        match = re.search(r"\b(?:including|include)\s+(.+?)(?:[.!?]|$)", text, re.I)
        if match:
            queries = _split_queries(match.group(1))
        elif count is None:
            match = re.search(r"\bwith\s+(.+?)(?:[.!?]|$)", text, re.I)
            if match:
                queries = _split_queries(match.group(1))
        return MentionCommand("create_poll", count=count, queries=queries)

    add_to_poll = re.search(
        r"\badd\s+(.+?)\s+(?:to|in)\s+(?:the\s+)?(?:current\s+|this week(?:'s)?\s+)?" + _POLL + r"\b",
        text,
        re.I,
    )
    if add_to_poll:
        return MentionCommand("add_to_poll", queries=_split_queries(add_to_poll.group(1)))

    add_to_pool = re.search(
        r"\badd\s+(.+?)\s+to\s+(?:the\s+)?(?:candidate\s+list|restaurant\s+pool|candidates?|pool)\b",
        text,
        re.I,
    )
    if add_to_pool:
        return MentionCommand("add_to_pool", queries=_split_queries(add_to_pool.group(1)))

    suggestion = re.search(r"\b(?:suggest|try|add)\s+(.+?)(?:[.!?]|$)", text, re.I)
    if suggestion:
        return MentionCommand("add_to_pool", queries=[_clean_query(suggestion.group(1))])

    # Backward compatibility: a bare mention is treated as a restaurant name.
    return MentionCommand("add_to_pool", queries=[text])


def match_restaurants(restaurants: list[Restaurant], query: str) -> list[Restaurant]:
    """Match a restaurant name first, then a cuisine such as ``pizza``."""
    needle = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    if not needle:
        return []

    def normal(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()

    exact = [restaurant for restaurant in restaurants if normal(restaurant.name) == needle]
    if exact:
        return exact
    query_words = set(needle.split())
    by_cuisine = [
        restaurant
        for restaurant in restaurants
        if normal(restaurant.cuisine) in query_words or normal(restaurant.cuisine) == needle
    ]
    if by_cuisine:
        return by_cuisine
    by_name = [restaurant for restaurant in restaurants if needle in normal(restaurant.name)]
    if by_name:
        return by_name
    return []
