"""Represent and validate conversational Slack commands.

Production routing is performed by an LLM, but its output is converted into the
small, typed command surface in this module before any handler can run.  The
legacy regex parser remains useful for read-only fallback behaviour and tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .models import Restaurant


@dataclass(frozen=True)
class MentionCommand:
    kind: str
    count: int | None = None
    queries: list[str] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    clarification: str | None = None


COMMAND_KINDS = frozenset(
    {
        "help",
        "list_restaurants",
        "list_poll",
        "restaurant_location",
        "create_poll",
        "add_to_poll",
        "add_to_pool",
        "conversation",
        "clarify",
    }
)
MUTATING_COMMAND_KINDS = frozenset({"create_poll", "add_to_poll", "add_to_pool"})


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


def parse_semantic_route(raw: str) -> MentionCommand:
    """Validate an LLM routing response and return a safe command.

    Any malformed response raises ``ValueError``.  Callers must treat that as a
    non-mutating routing failure rather than guessing an action.
    """
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("router returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("router response must be an object")

    intent = data.get("intent")
    mode = data.get("mode")
    if intent not in COMMAND_KINDS or mode not in {"execute", "answer"}:
        raise ValueError("router returned an unsupported intent or mode")

    def string_list(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list) or len(value) > 10:
            raise ValueError(f"router field {key!r} must be a short list")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 200:
                raise ValueError(f"router field {key!r} contains an invalid value")
            result.append(item.strip())
        return result

    names = string_list("restaurant_names")
    cuisines = string_list("cuisines")
    count = data.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValueError("router count must be an integer or null")
    flags = ("negated", "hypothetical", "ambiguous")
    if any(not isinstance(data.get(flag), bool) for flag in flags):
        raise ValueError("router safety flags must be booleans")
    clarification = data.get("clarification")
    if clarification is not None and not isinstance(clarification, str):
        raise ValueError("router clarification must be a string or null")

    # The model may identify an action while also recognizing that it was
    # negated, hypothetical, merely discussed, or ambiguous. Never execute it.
    unsafe_action = (
        intent in MUTATING_COMMAND_KINDS
        and (
            mode != "execute"
            or data["negated"]
            or data["hypothetical"]
            or data["ambiguous"]
        )
    )
    if unsafe_action:
        if data["ambiguous"]:
            return MentionCommand(
                "clarify",
                clarification=(
                    clarification or "Could you clarify what you want me to change?"
                ).strip(),
            )
        return MentionCommand("conversation")

    if intent in {"add_to_poll", "add_to_pool"} and not (names or cuisines):
        return MentionCommand(
            "clarify", clarification="Which restaurant or cuisine did you mean?"
        )
    if intent == "add_to_pool" and cuisines:
        return MentionCommand(
            "clarify",
            clarification="Please name the restaurant you want to add to the candidate list.",
        )
    if intent == "restaurant_location" and len(names) != 1:
        return MentionCommand("clarify", clarification="Which restaurant location do you mean?")
    if intent not in MUTATING_COMMAND_KINDS and mode == "execute":
        raise ValueError("read-only intent cannot use execute mode")

    return MentionCommand(
        intent,
        count=count,
        queries=names,
        cuisines=cuisines,
        clarification=clarification.strip() if isinstance(clarification, str) else None,
    )


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

    if (
        not text
        or re.search(r"\b(help|commands?|capabilities|services?)\b", lowered)
        or "what can you do" in lowered
        or "what do you do" in lowered
        or "who are you" in lowered
        or "introduce yourself" in lowered
    ):
        return MentionCommand("help")

    location_patterns = (
        r"\bwhere\s+is\s+(.+?)(?:[?!]|$)",
        r"\b(?:address|location)\s+(?:of|for)\s+(.+?)(?:[?!]|$)",
        r"\bgoogle\s+maps?\s+link\s+(?:for|to)\s+(.+?)(?:[?!]|$)",
    )
    for pattern in location_patterns:
        location = re.search(pattern, text, re.I)
        if location:
            return MentionCommand(
                "restaurant_location", queries=[_clean_query(location.group(1))]
            )

    if re.search(r"\b(list|show|what|which)\b.*\b(restaurants?|candidates?|pool)\b", lowered) and not re.search(
        _POLL, lowered
    ):
        return MentionCommand("list_restaurants")

    if re.search(r"\b(list|show|what|which)\b.*\b(?:current|this week(?:['’]s)?)?\s*" + _POLL + r"\b", lowered) or re.search(
        r"\b(?:current|this week(?:['’]s)?)\s+" + _POLL + r"\s+(?:choices?|options?)\b", lowered
    ):
        return MentionCommand("list_poll")

    # Check poll mutations before poll creation. Otherwise a phrase such as
    # "add Pizza Rustica to the open poll" can be misread as an "open poll"
    # creation request and replace the existing poll.
    add_to_poll = re.search(
        r"\badd\s+(.+?)\s+(?:to|in)\s+(?:the\s+)?"
        r"(?:current\s+|open\s+|existing\s+|this week(?:['’]s)?\s+)?"
        + _POLL
        + r"\b",
        text,
        re.I,
    )
    if add_to_poll:
        return MentionCommand("add_to_poll", queries=_split_queries(add_to_poll.group(1)))

    if re.search(r"\b(?:create|make|start|post)\b.*\b" + _POLL + r"\b", lowered):
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

    add_to_pool = re.search(
        r"\badd\s+(?:restaurant\s+)?(.+?)\s+to\s+(?:the\s+)?"
        r"(?:candidate\s+list|restaurant\s+(?:list|pool)|candidates?|list|pool)\b",
        text,
        re.I,
    )
    if add_to_pool:
        return MentionCommand("add_to_pool", queries=_split_queries(add_to_pool.group(1)))

    explicit_suggestion_patterns = (
        r"\brestaurant\s+suggestion\s*:\s*(.+?)(?:[.!?]|$)",
        r"\bsuggest\s+(?:the\s+)?restaurant\s+(.+?)(?:[.!?]|$)",
        r"\bsuggest\s+(.+?)\s+as\s+(?:a\s+)?restaurant(?:[.!?]|$)",
    )
    for pattern in explicit_suggestion_patterns:
        suggestion = re.search(pattern, text, re.I)
        if suggestion:
            return MentionCommand("add_to_pool", queries=[_clean_query(suggestion.group(1))])

    # Unknown text is deliberately non-mutating. Restaurant additions must use
    # an explicit verb such as "add", "suggest", or "try".
    return MentionCommand("conversation")


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
    by_cuisine = [
        restaurant
        for restaurant in restaurants
        if normal(restaurant.cuisine) == needle
    ]
    if by_cuisine:
        return by_cuisine
    by_name = [restaurant for restaurant in restaurants if needle in normal(restaurant.name)]
    if by_name:
        return by_name
    return []
