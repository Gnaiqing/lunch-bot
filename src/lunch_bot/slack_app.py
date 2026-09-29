"""Slack Bolt app wiring (Socket Mode).

Registers two kinds of handlers:

1. ``app_mention`` — a member @-mentions the bot to suggest a restaurant. The
   free text is parsed by the LLM, validated via Google Places, and added to the
   pool.
2. Poll button actions — a member clicks a "Vote" button; the vote is recorded
   and the poll message is refreshed with the new tally.

``slack_bolt`` is imported lazily inside :func:`build_app` so the rest of the
package (and the pure-logic tests) can be imported without slack-bolt installed.

TODO(user): create the Slack app, add the scopes documented in the README, enable
Socket Mode, and provide the tokens via environment variables.
"""

from __future__ import annotations

import random
import re

from . import db, polls
from .commands import MentionCommand, match_restaurants, parse_mention_command
from .config import Config
from .selection import select_candidates

# Matches a leading "<@U123ABC>" mention so we can strip it before parsing.
_MENTION_RE = re.compile(r"<@[^>]+>")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def build_app(config: Config, conn, llm=None):
    """Construct and return a configured slack_bolt ``App``.

    Args:
        config: Resolved configuration (Slack tokens required).
        conn: Open sqlite3 connection.
        llm: Optional :class:`lunch_bot.llm.LLMClient` for suggestion parsing.

    Returns:
        A ``slack_bolt.App`` instance ready to be driven by ``SocketModeHandler``.
    """
    from slack_bolt import App  # lazy import — optional dependency

    # Socket Mode authenticates via the app-level + bot tokens; the signing secret
    # is only used by an HTTP request receiver, which we don't run. Require just
    # the bot token here and pass the signing secret through only when it happens
    # to be set (it may be ``None``).
    config.require("slack_bot_token")

    app = App(
        token=config.slack_bot_token,
        signing_secret=config.slack_signing_secret,
    )

    # Import here to avoid a cycle at module load; discovery pulls in googlemaps
    # only when actually called.
    from .discovery import validate_suggestion

    def add_restaurant_to_pool(query: str):
        """Validate one free-text restaurant query and persist it."""
        name = query
        location_hint = None
        if llm is not None:
            try:
                parsed = llm.parse_suggestion(query)
                name = parsed.get("name") or query
                location_hint = parsed.get("location_hint")
            except Exception:  # pragma: no cover - provider best-effort
                pass

        try:
            restaurant = validate_suggestion(config, name, location_hint)
        except Exception as exc:  # pragma: no cover - network best-effort
            return None, f"Couldn't validate '{name}' right now: {exc}"
        if restaurant is None:
            return None, f"Hmm, I couldn't find '{name}' on Google Places within our budget."

        if llm is not None and not restaurant.cuisine:
            try:
                restaurant.cuisine = llm.classify_cuisine(restaurant.name, restaurant.address)
            except Exception:  # pragma: no cover - provider best-effort
                pass
        restaurant.id = db.upsert_restaurant(conn, restaurant)
        return restaurant, None

    def choose_from_pool(query: str, *, excluding: set[int] | None = None):
        """Resolve a name/cuisine query to one active restaurant."""
        excluding = excluding or set()
        matches = [
            restaurant
            for restaurant in match_restaurants(db.get_active_restaurants(conn), query)
            if restaurant.id not in excluding
        ]
        if not matches:
            return None
        return select_candidates(
            matches,
            n=1,
            rng=random.Random(),
            exploration_c=config.exploration_c,
        )[0]

    def restaurant_list_text() -> str:
        restaurants = sorted(
            db.get_active_restaurants(conn),
            key=lambda restaurant: ((restaurant.cuisine or "Other").casefold(), restaurant.name.casefold()),
        )
        if not restaurants:
            return "The candidate list is empty. Suggest one with `@lunch-bot add <restaurant> to the candidate list`."
        grouped: dict[str, list[str]] = {}
        for restaurant in restaurants:
            grouped.setdefault(restaurant.cuisine or "Other", []).append(restaurant.name)
        lines = [f"*Current restaurant candidates ({len(restaurants)}):*"]
        lines.extend(f"• *{cuisine}:* {', '.join(names)}" for cuisine, names in grouped.items())
        return "\n".join(lines)

    def current_poll_text() -> str:
        poll = db.get_open_poll(conn, config.slack_channel_id)
        if poll is None:
            return "There is no open lunch poll right now."
        option_ids = db.get_poll_option_ids(conn, poll["id"])
        tally = db.tally_votes(conn, poll["id"])
        lines = ["*Current lunch poll:*"]
        for restaurant_id in option_ids:
            restaurant = db.get_restaurant(conn, restaurant_id)
            if restaurant is not None:
                count = tally.get(restaurant_id, 0)
                lines.append(f"• {restaurant.name} — {count} vote{'s' if count != 1 else ''}")
        return "\n".join(lines)

    def resolve_or_add(query: str, *, excluding: set[int] | None = None):
        restaurant = choose_from_pool(query, excluding=excluding)
        if restaurant is not None:
            return restaurant, None
        return add_restaurant_to_pool(query)

    def handle_create_poll(command: MentionCommand, say, client) -> None:
        if command.count is not None:
            count = command.count
        elif len(command.queries) >= 2:
            # "Create a poll with A, B, and C" naturally means those three
            # choices; a single named inclusion still fills to the configured size.
            count = len(command.queries)
        else:
            count = config.poll_size
        if not 2 <= count <= 10:
            say("Please request between 2 and 10 poll choices.")
            return

        required_ids: list[int] = []
        unresolved: list[str] = []
        for query in command.queries:
            restaurant, error = resolve_or_add(query, excluding=set(required_ids))
            if restaurant is None:
                unresolved.append(error or query)
            elif restaurant.id is not None:
                required_ids.append(restaurant.id)
        if unresolved:
            say("I couldn't resolve every requested choice:\n• " + "\n• ".join(unresolved))
            return

        from .scheduler import create_weekly_poll

        poll_id = create_weekly_poll(
            config,
            conn,
            client,
            llm=llm,
            poll_size=count,
            required_restaurant_ids=required_ids,
        )
        if poll_id is None:
            say("I couldn't create the poll. Check the bot logs and make sure the candidate list is not empty.")

    def handle_add_to_poll(command: MentionCommand, say, client) -> None:
        poll = db.get_open_poll(conn, config.slack_channel_id)
        if poll is None:
            say("There is no open poll. Create one first with `@lunch-bot create a poll with 4 choices`.")
            return
        if not poll["slack_ts"]:
            say("The open poll has no Slack message to update.")
            return

        existing_ids = set(db.get_poll_option_ids(conn, poll["id"]))
        added_ids: list[int] = []
        failures: list[str] = []
        for query in command.queries:
            restaurant, error = resolve_or_add(query, excluding=existing_ids | set(added_ids))
            if restaurant is None or restaurant.id is None:
                failures.append(error or f"Couldn't resolve '{query}'.")
                continue
            if db.add_poll_option_if_open(conn, poll["id"], restaurant.id):
                added_ids.append(restaurant.id)
            else:
                failures.append(f"'{restaurant.name}' is already in the poll or the poll is closed.")

        if not added_ids:
            say("\n".join(failures) if failures else "No new choices were added.")
            return

        option_ids = db.get_poll_option_ids(conn, poll["id"])
        restaurants = [db.get_restaurant(conn, restaurant_id) for restaurant_id in option_ids]
        restaurants = [restaurant for restaurant in restaurants if restaurant is not None]
        try:
            client.chat_update(
                channel=config.slack_channel_id,
                ts=poll["slack_ts"],
                blocks=polls.build_poll_blocks(
                    poll["id"], restaurants, tally=db.tally_votes(conn, poll["id"])
                ),
                text="Lunch poll updated",
            )
        except Exception as exc:  # pragma: no cover - Slack network best-effort
            for restaurant_id in added_ids:
                db.remove_poll_option(conn, poll["id"], restaurant_id)
            say(f"I couldn't update the Slack poll, so I rolled back the new choices: {exc}")
            return

        db.increment_selection_counts(conn, added_ids)
        names = [db.get_restaurant(conn, restaurant_id).name for restaurant_id in added_ids]
        message = "Added to the current poll: *" + "*, *".join(names) + "*."
        if failures:
            message += "\n" + "\n".join(failures)
        say(message)

    @app.event("app_mention")
    def handle_app_mention(event, say, client):
        """Handle conversational poll management and restaurant requests."""
        text = _strip_mentions(event.get("text", ""))
        command = parse_mention_command(text)

        if command.kind in {"create_poll", "add_to_poll"} and event.get("channel") != config.slack_channel_id:
            say(f"Polls can only be managed in {config.slack_channel_name}.")
            return

        if command.kind == "help":
            say(
                "I manage our lunch candidate pool and multi-select polls. Try:\n"
                "• `@lunch-bot show current restaurants`\n"
                "• `@lunch-bot add Pai Northern Thai to the candidate list`\n"
                "• `@lunch-bot create a poll with 4 choices`\n"
                "• `@lunch-bot create a poll with 4 choices including Pala 148`\n"
                "• `@lunch-bot add a pizza restaurant to this week's poll`\n"
                "• `@lunch-bot show the current poll`\n"
                "Poll votes are multi-select; click a choice again to remove that vote."
            )
            return
        if command.kind == "list_restaurants":
            say(restaurant_list_text())
            return
        if command.kind == "list_poll":
            say(current_poll_text())
            return
        if command.kind == "create_poll":
            handle_create_poll(command, say, client)
            return
        if command.kind == "add_to_poll":
            handle_add_to_poll(command, say, client)
            return

        successes: list[str] = []
        failures: list[str] = []
        for query in command.queries:
            restaurant, error = add_restaurant_to_pool(query)
            if restaurant is None:
                failures.append(error or f"Couldn't add '{query}'.")
            else:
                label = f"*{restaurant.name}*"
                if restaurant.cuisine:
                    label += f" ({restaurant.cuisine})"
                successes.append(label)
        response = ""
        if successes:
            response = "Added " + ", ".join(successes) + " to the lunch candidate list. 🍜"
        if failures:
            response += ("\n" if response else "") + "\n".join(failures)
        say(response or "I couldn't understand that request. Mention me with `help` for examples.")

    @app.action(re.compile(r"^vote::"))
    def handle_vote_action(ack, body, client):
        """Handle a poll vote button click: record it and refresh the message."""
        ack()
        action = body["actions"][0]
        user_id = body["user"]["id"]
        result = polls.handle_vote(conn, action["action_id"], user_id)
        if not result:
            return
        poll_id, _ = result

        # Refresh the poll message with the updated tally.
        option_ids = db.get_poll_option_ids(conn, poll_id)
        restaurants = [db.get_restaurant(conn, rid) for rid in option_ids]
        restaurants = [r for r in restaurants if r is not None]
        tally = db.tally_votes(conn, poll_id)
        blocks = polls.build_poll_blocks(poll_id, restaurants, tally=tally)

        container = body.get("container", {})
        channel = body.get("channel", {}).get("id") or config.slack_channel_id
        ts = container.get("message_ts")
        if channel and ts:
            client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Lunch poll updated")

    return app
