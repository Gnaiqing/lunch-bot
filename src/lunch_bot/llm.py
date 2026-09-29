"""Anthropic (Claude) client wrapper.

Two small jobs, both well-suited to a fast/cheap Haiku-class model:

1. **Cuisine tagging** — classify a restaurant into a single cuisine label.
2. **Suggestion parsing** — extract a restaurant name (and optional location
   hint) from a free-text Slack message that @-mentions the bot.

Model IDs and SDK usage follow the ``claude-api`` skill guidance. The default
model is ``claude-haiku-4-5`` (the current Haiku-class model), configurable via
:class:`lunch_bot.config.Config`.

The ``anthropic`` import is done lazily inside :class:`LLMClient` so that modules
which merely import this file (or the wider package) don't require the SDK to be
installed unless the LLM is actually used.
"""

from __future__ import annotations

import json
from typing import Optional

# A compact, stable set of cuisine labels. Kept small so the poll can span
# clearly-distinct cuisines; extend as the pool grows.
CUISINE_LABELS = [
    "italian", "japanese", "chinese", "thai", "vietnamese", "korean", "indian",
    "mexican", "middle_eastern", "mediterranean", "greek", "american",
    "caribbean", "ethiopian", "french", "pizza", "burgers", "sushi", "vegan",
    "cafe", "bakery", "seafood", "bbq", "other",
]


class LLMClient:
    """Thin wrapper over the Anthropic Messages API for tagging + parsing."""

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-haiku-4-5"):
        """Create the client.

        Args:
            api_key: Anthropic API key. If ``None``, the SDK resolves credentials
                from the environment (``ANTHROPIC_API_KEY`` etc.).
            model: Model ID; defaults to the current Haiku-class model.
        """
        import anthropic  # lazy import — optional dependency

        self.model = model
        # Passing api_key=None lets the SDK use its normal env-based resolution.
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def _complete_text(self, system: str, user: str, *, max_tokens: int = 256) -> str:
        """Run a single non-streaming completion and return concatenated text."""
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()

    def classify_cuisine(self, name: str, address: Optional[str] = None) -> str:
        """Return a single cuisine label for a restaurant.

        Falls back to ``"other"`` if the model returns something unexpected.
        """
        system = (
            "You classify a restaurant into exactly ONE cuisine label from this "
            "list: " + ", ".join(CUISINE_LABELS) + ". "
            "Respond with only the single label, lowercase, no punctuation."
        )
        user = f"Restaurant name: {name}"
        if address:
            user += f"\nAddress: {address}"
        raw = self._complete_text(system, user, max_tokens=16).lower().strip()
        # Normalise: pick the first known label that appears in the reply.
        if raw in CUISINE_LABELS:
            return raw
        for label in CUISINE_LABELS:
            if label in raw:
                return label
        return "other"

    def parse_suggestion(self, text: str) -> dict:
        """Extract a restaurant suggestion from a free-text Slack message.

        Returns a dict with keys:
            - ``name``: the restaurant name, or ``None`` if none found.
            - ``location_hint``: optional area/address hint, or ``None``.

        The bot @-mention is typically already stripped by the caller, but the
        prompt tolerates leftover mention text.
        """
        system = (
            "Extract a restaurant suggestion from the user's message. "
            "Return ONLY a JSON object with keys \"name\" (string or null) and "
            "\"location_hint\" (string or null). If no restaurant is being "
            "suggested, set name to null. Do not include any prose."
        )
        raw = self._complete_text(system, text, max_tokens=128)
        return _safe_parse_suggestion(raw)


def _safe_parse_suggestion(raw: str) -> dict:
    """Parse the model's JSON reply defensively into a suggestion dict."""
    result = {"name": None, "location_hint": None}
    if not raw:
        return result
    # Strip common code-fence wrapping if present.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        # Drop a leading "json" language tag if present.
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return result
    if isinstance(data, dict):
        name = data.get("name")
        loc = data.get("location_hint")
        result["name"] = name if isinstance(name, str) and name.strip() else None
        result["location_hint"] = loc if isinstance(loc, str) and loc.strip() else None
    return result
