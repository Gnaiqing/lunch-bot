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
import uuid

from . import db, polls
from .commands import (
    MUTATING_COMMAND_KINDS,
    MentionCommand,
    parse_mention_command,
)
from .config import Config
from .models import Restaurant
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
    from .discovery import google_maps_url, validate_suggestion

    def find_restaurant_suggestion(query: str):
        """Resolve a query through Google Places without persisting anything."""
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

        return restaurant, None

    def request_restaurant_confirmation(
        query: str,
        event: dict,
        client,
        *,
        target: str,
        poll_id: int | None = None,
    ) -> tuple[bool, str | None]:
        """Post a confirmation card while keeping the match out of the pool."""
        restaurant, error = find_restaurant_suggestion(query)
        if restaurant is None:
            return False, error
        user_id = event.get("user")
        channel = event.get("channel")
        if not user_id or not channel:
            return False, "I couldn't identify the requesting user or channel."

        token = uuid.uuid4().hex
        db.create_pending_restaurant_confirmation(
            conn,
            token=token,
            slack_user_id=user_id,
            slack_channel=channel,
            query=query,
            target=target,
            poll_id=poll_id,
            restaurant=restaurant,
        )
        details = [f"*{restaurant.name}*"]
        if restaurant.address:
            details.append(restaurant.address)
        if restaurant.price_level is not None:
            details.append(f"Google price level: {restaurant.price_level}/4")
        maps_url = restaurant.maps_url or google_maps_url(
            restaurant.name, restaurant.place_id
        )
        details.append(f"<{maps_url}|Open in Google Maps>")
        destination = "the candidate list"
        if target == "poll":
            destination += " and the current poll"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "I found this Google Maps result:\n" + "\n".join(details),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Is this the restaurant you meant? It will only be added to {destination} after you confirm.",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "action_id": "restaurant_confirm",
                        "value": token,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Not this one"},
                        "action_id": "restaurant_cancel",
                        "value": token,
                    },
                ],
            },
        ]
        try:
            client.chat_postMessage(
                channel=channel,
                text=f"Please confirm the Google Maps match for {restaurant.name}.",
                blocks=blocks,
            )
        except Exception:
            claimed = db.claim_pending_restaurant_confirmation(conn, token, user_id)
            if claimed is not None:
                db.resolve_pending_restaurant_confirmation(conn, token, "failed")
            return False, "I found a match but couldn't post the confirmation card."
        return True, None

    def choose_from_pool(
        query: str,
        *,
        excluding: set[int] | None = None,
        cuisine: bool = False,
    ) -> tuple[Restaurant | None, list[str]]:
        """Resolve one entity, returning ambiguity instead of guessing a name."""
        excluding = excluding or set()
        available = [
            restaurant for restaurant in db.get_active_restaurants(conn)
            if restaurant.id not in excluding
        ]
        if cuisine:
            needle = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
            matches = [
                restaurant for restaurant in available
                if re.sub(r"[^a-z0-9]+", " ", (restaurant.cuisine or "").casefold()).strip()
                == needle
            ]
        else:
            needle = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
            def normalized(value: str) -> str:
                return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

            matches = [
                restaurant
                for restaurant in available
                if normalized(restaurant.name) == needle
            ]
            if not matches:
                matches = [
                    restaurant
                    for restaurant in available
                    if needle in normalized(restaurant.name)
                ]
        if not matches:
            return None, []
        if not cuisine and len(matches) > 1:
            return None, [restaurant.name for restaurant in matches]
        selected = select_candidates(
            matches,
            n=1,
            rng=random.Random(),
            exploration_c=config.exploration_c,
        )[0]
        return selected, []

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

    def conversation_context() -> str:
        restaurants = sorted(
            db.get_active_restaurants(conn), key=lambda restaurant: restaurant.name.casefold()
        )
        names = ", ".join(
            f"{restaurant.name} [{restaurant.cuisine or 'Other'}]"
            for restaurant in restaurants
        )
        return (
            f"Configured channel: {config.slack_channel_name}.\n"
            f"Weekly schedule: poll {config.schedule['poll_create'].day} "
            f"{config.schedule['poll_create'].time_str}, close "
            f"{config.schedule['poll_close'].day} {config.schedule['poll_close'].time_str}, "
            f"timezone {config.timezone}.\n"
            f"Candidate restaurants ({len(restaurants)}): {names or 'none'}.\n"
            f"{current_poll_text()}\n"
            "Supported mutations require explicit commands: create a poll, add a named "
            "restaurant to the candidate list, or add a restaurant/cuisine to the open poll."
        )

    def update_poll_message(poll_id: int, client) -> bool:
        poll = db.get_poll(conn, poll_id)
        if poll is None or poll["status"] != "open" or not poll["slack_ts"]:
            return False
        option_ids = db.get_poll_option_ids(conn, poll_id)
        restaurants = [db.get_restaurant(conn, restaurant_id) for restaurant_id in option_ids]
        restaurants = [restaurant for restaurant in restaurants if restaurant is not None]
        client.chat_update(
            channel=poll["slack_channel"],
            ts=poll["slack_ts"],
            blocks=polls.build_poll_blocks(
                poll_id,
                restaurants,
                tally=db.tally_votes(conn, poll_id),
                voters=db.get_poll_voters(conn, poll_id),
            ),
            text="Lunch poll updated",
        )
        return True

    def handle_create_poll(command: MentionCommand, event, say, client) -> None:
        existing_poll = db.get_open_poll(conn, config.slack_channel_id)
        if existing_poll is not None:
            say(
                "There is already an open lunch poll, so I left it and its votes unchanged. "
                "Add choices with `@lunch-bot add <restaurant or cuisine> to the open poll`."
            )
            return
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
        entities = [(query, False) for query in command.queries]
        entities.extend((query, True) for query in command.cuisines)
        for query, is_cuisine in entities:
            restaurant, ambiguous = choose_from_pool(
                query, excluding=set(required_ids), cuisine=is_cuisine
            )
            if ambiguous:
                unresolved.append(
                    f"'{query}' matches multiple restaurants: {', '.join(ambiguous)}. "
                    "Please use the full name."
                )
                continue
            if restaurant is not None and restaurant.id is not None:
                required_ids.append(restaurant.id)
                continue
            if is_cuisine:
                unresolved.append(
                    f"There are no available {query} restaurants in the candidate list."
                )
                continue
            requested, error = request_restaurant_confirmation(
                query, event, client, target="pool"
            )
            if requested:
                unresolved.append(
                    f"Confirm the Google Maps match for '{query}', then request the poll again."
                )
            else:
                unresolved.append(error or query)
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

    def handle_add_to_poll(command: MentionCommand, event, say, client) -> None:
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
        entities = [(query, False) for query in command.queries]
        entities.extend((query, True) for query in command.cuisines)
        for query, is_cuisine in entities:
            restaurant, ambiguous = choose_from_pool(
                query,
                excluding=existing_ids | set(added_ids),
                cuisine=is_cuisine,
            )
            if ambiguous:
                failures.append(
                    f"'{query}' matches multiple restaurants: {', '.join(ambiguous)}. "
                    "Please use the full name."
                )
                continue
            if restaurant is None:
                if is_cuisine:
                    failures.append(
                        f"There are no available {query} restaurants in the candidate list."
                    )
                    continue
                requested, error = request_restaurant_confirmation(
                    query, event, client, target="poll", poll_id=poll["id"]
                )
                if requested:
                    failures.append(
                        f"Waiting for your confirmation before adding '{query}' to the poll."
                    )
                else:
                    failures.append(error or f"Couldn't resolve '{query}'.")
                continue
            if db.add_poll_option_if_open(conn, poll["id"], restaurant.id):
                added_ids.append(restaurant.id)
            else:
                failures.append(f"'{restaurant.name}' is already in the poll or the poll is closed.")

        if not added_ids:
            say("\n".join(failures) if failures else "No new choices were added.")
            return

        try:
            updated = update_poll_message(poll["id"], client)
        except Exception as exc:  # pragma: no cover - Slack network best-effort
            for restaurant_id in added_ids:
                db.remove_poll_option(conn, poll["id"], restaurant_id)
            say(f"I couldn't update the Slack poll, so I rolled back the new choices: {exc}")
            return
        if not updated:
            for restaurant_id in added_ids:
                db.remove_poll_option(conn, poll["id"], restaurant_id)
            say("The poll changed while I was updating it, so I rolled back the new choices.")
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
        if llm is not None:
            try:
                command = llm.route_message(text, conversation_context())
            except Exception:  # pragma: no cover - provider/network best-effort
                say(
                    "I couldn't safely understand that request, so I did not change anything. "
                    "Please try again or mention me with `help`."
                )
                return
        else:
            # Keep useful read-only behaviour when the model is unavailable, but
            # never authorize a mutation through the legacy regex parser.
            command = parse_mention_command(text)
            if command.kind in MUTATING_COMMAND_KINDS:
                say(
                    "Language routing is unavailable, so I did not change anything. "
                    "Please try again when the LLM service is enabled."
                )
                return

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
                "New Google Maps matches require your confirmation before they are added. "
                "Poll votes are multi-select; click a choice again to remove that vote."
            )
            return
        if command.kind == "list_restaurants":
            say(restaurant_list_text())
            return
        if command.kind == "list_poll":
            say(current_poll_text())
            return
        if command.kind == "restaurant_location":
            restaurant, ambiguous = choose_from_pool(command.queries[0])
            if ambiguous:
                say(
                    f"'{command.queries[0]}' matches multiple restaurants: "
                    + ", ".join(ambiguous)
                    + ". Please ask using the full name."
                )
                return
            if restaurant is None:
                say(f"I couldn't find '{command.queries[0]}' in the candidate list.")
                return
            maps_url = restaurant.maps_url or google_maps_url(
                restaurant.name, restaurant.place_id
            )
            address = restaurant.address or "Address not yet available"
            say(f"*{restaurant.name}*\n{address}\n<{maps_url}|Open in Google Maps>")
            return
        if command.kind == "create_poll":
            handle_create_poll(command, event, say, client)
            return
        if command.kind == "add_to_poll":
            handle_add_to_poll(command, event, say, client)
            return
        if command.kind == "conversation":
            if llm is None:
                say(
                    "I'm Lunch Bot. I manage restaurant candidates and weekly lunch polls. "
                    "Conversational QA is unavailable right now, but I did not change anything. "
                    "Mention me with `help` to see commands."
                )
                return
            try:
                answer = llm.answer_question(text, conversation_context())
            except Exception:  # pragma: no cover - provider best-effort
                answer = ""
            say(
                answer
                or "I couldn't answer that right now, but I did not change anything. "
                "Mention me with `help` to see commands."
            )
            return

        if command.kind == "clarify":
            say(command.clarification or "Could you clarify what you want me to do?")
            return

        already_available: list[str] = []
        pending: list[str] = []
        failures: list[str] = []
        for query in command.queries:
            existing, ambiguous = choose_from_pool(query)
            if ambiguous:
                failures.append(
                    f"'{query}' matches multiple restaurants: {', '.join(ambiguous)}. "
                    "Please use the full name."
                )
                continue
            if existing is not None:
                already_available.append(existing.name)
                continue
            requested, error = request_restaurant_confirmation(
                query, event, client, target="pool"
            )
            if not requested:
                failures.append(error or f"Couldn't add '{query}'.")
            else:
                pending.append(query)
        response_parts: list[str] = []
        if already_available:
            response_parts.append(
                "Already in the candidate list: *" + "*, *".join(already_available) + "*."
            )
        if pending:
            response_parts.append(
                "I found Google Maps matches for "
                + ", ".join(f"'{query}'" for query in pending)
                + ". Please use the confirmation card"
                + ("s" if len(pending) != 1 else "")
                + " above; nothing has been added yet."
            )
        if failures:
            response_parts.extend(failures)
        if response_parts:
            say("\n".join(response_parts))

    def action_message_location(body: dict) -> tuple[str | None, str | None]:
        channel = body.get("channel", {}).get("id")
        ts = body.get("container", {}).get("message_ts")
        return channel, ts

    def action_error(client, body: dict, text: str) -> None:
        channel, _ = action_message_location(body)
        user_id = body.get("user", {}).get("id")
        if channel and user_id:
            client.chat_postEphemeral(channel=channel, user=user_id, text=text)

    @app.action("restaurant_confirm")
    def handle_restaurant_confirm(ack, body, client):
        """Persist a Google Places match only after its requester confirms it."""
        ack()
        action = body.get("actions", [{}])[0]
        token = action.get("value", "")
        user_id = body.get("user", {}).get("id")
        pending = db.get_pending_restaurant_confirmation(conn, token)
        if pending is None:
            action_error(client, body, "This restaurant confirmation no longer exists.")
            return
        if pending["slack_user_id"] != user_id:
            action_error(client, body, "Only the person who requested this restaurant can confirm it.")
            return
        claimed = db.claim_pending_restaurant_confirmation(conn, token, user_id)
        if claimed is None:
            action_error(client, body, "This restaurant confirmation has already been handled.")
            return

        restaurant = Restaurant(
            name=claimed["name"],
            cuisine=claimed["cuisine"],
            address=claimed["address"],
            place_id=claimed["place_id"],
            lat=claimed["lat"],
            lng=claimed["lng"],
            maps_url=claimed["maps_url"],
            price_level=claimed["price_level"],
            source=claimed["source"],
        )
        try:
            if llm is not None and not restaurant.cuisine:
                try:
                    restaurant.cuisine = llm.classify_cuisine(
                        restaurant.name, restaurant.address
                    )
                except Exception:  # pragma: no cover - provider best-effort
                    pass
            restaurant.id = db.upsert_restaurant(conn, restaurant)
            poll_added = False
            if claimed["target"] == "poll" and claimed["poll_id"] is not None:
                poll_added = db.add_poll_option_if_open(
                    conn, claimed["poll_id"], restaurant.id
                )
                if poll_added:
                    try:
                        updated = update_poll_message(claimed["poll_id"], client)
                    except Exception:
                        db.remove_poll_option(conn, claimed["poll_id"], restaurant.id)
                        poll_added = False
                    else:
                        poll_added = updated
                        if poll_added:
                            db.increment_selection_counts(conn, [restaurant.id])
            db.resolve_pending_restaurant_confirmation(conn, token, "confirmed")
        except Exception:
            db.resolve_pending_restaurant_confirmation(conn, token, "failed")
            raise

        message = f"Confirmed and added *{restaurant.name}* to the candidate list."
        if claimed["target"] == "poll":
            if poll_added:
                message += " It was also added to the current poll."
            else:
                message += " The requested poll was no longer available to update."
        channel, ts = action_message_location(body)
        if channel and ts:
            client.chat_update(
                channel=channel,
                ts=ts,
                text=message,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
            )

    @app.action("restaurant_cancel")
    def handle_restaurant_cancel(ack, body, client):
        """Cancel a pending match without changing the restaurant pool."""
        ack()
        token = body.get("actions", [{}])[0].get("value", "")
        user_id = body.get("user", {}).get("id")
        pending = db.get_pending_restaurant_confirmation(conn, token)
        if pending is None:
            action_error(client, body, "This restaurant confirmation no longer exists.")
            return
        if pending["slack_user_id"] != user_id:
            action_error(client, body, "Only the person who requested this restaurant can cancel it.")
            return
        claimed = db.claim_pending_restaurant_confirmation(conn, token, user_id)
        if claimed is None:
            action_error(client, body, "This restaurant confirmation has already been handled.")
            return
        db.resolve_pending_restaurant_confirmation(conn, token, "cancelled")
        message = f"Cancelled. *{claimed['name']}* was not added."
        channel, ts = action_message_location(body)
        if channel and ts:
            client.chat_update(
                channel=channel,
                ts=ts,
                text=message,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
            )

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
        voters = db.get_poll_voters(conn, poll_id)
        blocks = polls.build_poll_blocks(
            poll_id, restaurants, tally=tally, voters=voters
        )

        container = body.get("container", {})
        channel = body.get("channel", {}).get("id") or config.slack_channel_id
        ts = container.get("message_ts")
        if channel and ts:
            client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Lunch poll updated")

    return app
