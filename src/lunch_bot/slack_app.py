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

import re

from . import db, polls
from .config import Config

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

    @app.event("app_mention")
    def handle_app_mention(event, say):
        """Parse a restaurant suggestion from an @-mention and add it to the pool."""
        text = _strip_mentions(event.get("text", ""))
        if not text:
            say("Mention me with a restaurant to suggest, e.g. `@lunchbot try Pai Northern Thai`.")
            return

        name = None
        location_hint = None
        if llm is not None:
            try:
                parsed = llm.parse_suggestion(text)
                name = parsed.get("name")
                location_hint = parsed.get("location_hint")
            except Exception:  # pragma: no cover - LLM best-effort
                # A transient provider failure must not abort the handler; fall
                # back to the non-LLM path (treat the whole message as the name).
                name = None
                location_hint = None
        if not name:
            # Fall back to treating the whole message as the name.
            name = text

        try:
            restaurant = validate_suggestion(config, name, location_hint)
        except Exception as exc:  # pragma: no cover - network best-effort
            say(f"Couldn't validate '{name}' right now: {exc}")
            return

        if restaurant is None:
            say(f"Hmm, I couldn't find '{name}' on Google Places within our budget.")
            return

        if llm is not None and not restaurant.cuisine:
            try:
                restaurant.cuisine = llm.classify_cuisine(restaurant.name, restaurant.address)
            except Exception:  # pragma: no cover
                pass

        db.upsert_restaurant(conn, restaurant)
        say(
            f"Added *{restaurant.name}*"
            + (f" ({restaurant.cuisine})" if restaurant.cuisine else "")
            + " to the lunch pool. 🍜"
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
        blocks = polls.build_poll_blocks(poll_id, restaurants, tally=tally)

        container = body.get("container", {})
        channel = body.get("channel", {}).get("id") or config.slack_channel_id
        ts = container.get("message_ts")
        if channel and ts:
            client.chat_update(channel=channel, ts=ts, blocks=blocks, text="Lunch poll updated")

    return app
