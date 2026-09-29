"""LLM client wrappers for cuisine tagging + suggestion parsing.

Two small jobs, both well-suited to a fast/cheap model:

1. **Cuisine tagging** — classify a restaurant into a single cuisine label.
2. **Suggestion parsing** — extract a restaurant name (and optional location
   hint) from a free-text Slack message that @-mentions the bot.

Two providers are supported behind one provider-agnostic interface: **Anthropic**
(Claude) and **OpenAI**. The shared prompt-building and response-parsing logic
lives on the :class:`LLMClient` base class; each backend only implements a small
``_complete_text`` method. :func:`build_llm_client` returns the right backend for
``config.llm_provider``.

Anthropic model IDs and the Messages API usage follow the ``claude-api`` skill
guidance; the default Anthropic model is ``claude-haiku-4-5`` (the current
Haiku-class model). The default OpenAI model is ``gpt-4o-mini`` (small/cheap).

The provider SDK imports (``anthropic`` / ``openai``) are done lazily inside each
backend's constructor so that modules which merely import this file (or the wider
package) don't require an SDK to be installed unless the LLM is actually used.
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

# Default per-provider models. Anthropic: current Haiku-class model (fast/cheap,
# per the ``claude-api`` skill). OpenAI: a small, cheap current model.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class LLMClient:
    """Provider-agnostic base: the shared tagging + parsing logic.

    Concrete backends (:class:`AnthropicClient`, :class:`OpenAIClient`) implement
    :meth:`_complete_text` for a specific provider. The cuisine/suggestion prompt
    building and response parsing live here so they are defined exactly once.
    """

    def __init__(self, model: str):
        self.model = model

    def _complete_text(self, system: str, user: str, *, max_tokens: int = 256) -> str:
        """Run a single non-streaming completion and return the plain text reply."""
        raise NotImplementedError

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

    def answer_question(self, text: str, context: str) -> str:
        """Answer a read-only conversational question about Lunch Bot.

        This method may explain state and supported commands, but it must never
        be used to authorize or perform a mutation. Command routing remains
        deterministic in :mod:`lunch_bot.commands`.
        """
        system = (
            "You are Lunch Bot, a concise and friendly assistant for a reading "
            "group's lunch polls. Answer the user's question using the supplied "
            "context. Never claim that you created a poll, added a restaurant, "
            "cast a vote, or changed any state. If the user wants an action, "
            "explain the explicit command they should use. Do not invent "
            "restaurants, votes, schedules, or capabilities."
        )
        user = f"Current Lunch Bot context:\n{context}\n\nUser message:\n{text}"
        return self._complete_text(system, user, max_tokens=300).strip()


class AnthropicClient(LLMClient):
    """LLM backend over the Anthropic Messages API."""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_ANTHROPIC_MODEL):
        """Create the client.

        Args:
            api_key: Anthropic API key. If ``None``, the SDK resolves credentials
                from the environment (``ANTHROPIC_API_KEY`` etc.).
            model: Model ID; defaults to the current Haiku-class model.
        """
        import anthropic  # lazy import — optional dependency

        super().__init__(model)
        # Passing api_key=None lets the SDK use its normal env-based resolution.
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def _complete_text(self, system: str, user: str, *, max_tokens: int = 256) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,  # deterministic tagging/parsing
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()


class OpenAIClient(LLMClient):
    """LLM backend over the OpenAI Chat Completions API."""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_OPENAI_MODEL):
        """Create the client.

        Args:
            api_key: OpenAI API key. If ``None``, the SDK resolves credentials
                from the environment (``OPENAI_API_KEY`` etc.).
            model: Model ID; defaults to a small, cheap current model.
        """
        from openai import OpenAI  # lazy import — optional dependency

        super().__init__(model)
        # Passing api_key=None lets the SDK use its normal env-based resolution.
        self._client = OpenAI(api_key=api_key) if api_key else OpenAI()

    def _complete_text(self, system: str, user: str, *, max_tokens: int = 256) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,  # deterministic tagging/parsing
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content or ""
        return content.strip()


# Provider name -> backend class. Used by the factory and (in tests) to assert
# the class selection without instantiating a network client.
_PROVIDER_CLASSES: dict[str, type[LLMClient]] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
}


def llm_client_class(provider: str) -> type[LLMClient]:
    """Return the backend class for ``provider`` without instantiating it.

    Raises ``ValueError`` on an unknown provider.
    """
    try:
        return _PROVIDER_CLASSES[provider]
    except KeyError:
        raise ValueError(
            f"Unknown llm_provider: {provider!r}. "
            f"Use one of: {', '.join(sorted(_PROVIDER_CLASSES))}."
        ) from None


def build_llm_client(config) -> LLMClient:
    """Return the LLM backend for ``config.llm_provider``, wired with its key+model.

    The selected provider's API key and model are read from ``config``; the other
    provider's settings are ignored. The heavy SDK import happens lazily inside the
    chosen backend's constructor.
    """
    cls = llm_client_class(config.llm_provider)
    if config.llm_provider == "openai":
        return cls(api_key=config.openai_api_key, model=config.openai_model)
    return cls(api_key=config.anthropic_api_key, model=config.anthropic_model)


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
