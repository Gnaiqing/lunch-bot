"""Tests for LLM provider selection, the client factory, and startup key logic.

Network-free: no real API calls. The factory's class selection is asserted
without instantiating a network client (via :func:`llm_client_class`), and the
Anthropic backend is only *constructed* (no request is made).
"""

import pytest
import json

from lunch_bot.config import load_config
from lunch_bot.llm import (
    AnthropicClient,
    LLMClient,
    OpenAIClient,
    build_llm_client,
    llm_client_class,
)
from lunch_bot.main import active_llm_api_key, missing_llm_key_env


class _CapturingClient(LLMClient):
    def __init__(self):
        super().__init__("test-model")
        self.call = None

    def _complete_text(self, system, user, *, max_tokens=256):
        self.call = (system, user, max_tokens)
        return "I manage lunch polls."


class _RoutingClient(LLMClient):
    def __init__(self, payload):
        super().__init__("test-model")
        self.payload = payload
        self.call = None

    def _complete_text(self, system, user, *, max_tokens=256):
        self.call = (system, user, max_tokens)
        return json.dumps(self.payload)


def _cfg(tmp_path, env):
    return load_config(config_path=str(tmp_path / "none.yaml"), env=env, load_dotenv=False)


# --- config: llm_provider parsing (default / override / invalid) ---


def test_provider_defaults_to_anthropic(tmp_path):
    cfg = _cfg(tmp_path, {})
    assert cfg.llm_provider == "anthropic"
    assert cfg.anthropic_model == "claude-haiku-4-5"
    assert cfg.openai_model == "gpt-4o-mini"


def test_provider_override_and_models(tmp_path):
    cfg = _cfg(
        tmp_path,
        {"LLM_PROVIDER": "OpenAI", "OPENAI_MODEL": "gpt-4o", "ANTHROPIC_MODEL": "claude-x"},
    )
    assert cfg.llm_provider == "openai"  # normalised to lowercase
    assert cfg.openai_model == "gpt-4o"
    assert cfg.anthropic_model == "claude-x"


def test_legacy_llm_model_still_sets_anthropic_model(tmp_path):
    cfg = _cfg(tmp_path, {"LLM_MODEL": "claude-legacy"})
    assert cfg.anthropic_model == "claude-legacy"


def test_invalid_provider_raises(tmp_path):
    with pytest.raises(ValueError) as exc:
        _cfg(tmp_path, {"LLM_PROVIDER": "gemini"})
    assert "llm_provider" in str(exc.value)


# --- factory: correct backend class per provider (no network client) ---


def test_llm_client_class_selection():
    assert llm_client_class("anthropic") is AnthropicClient
    assert llm_client_class("openai") is OpenAIClient
    assert issubclass(AnthropicClient, LLMClient)
    assert issubclass(OpenAIClient, LLMClient)


def test_llm_client_class_invalid_raises():
    with pytest.raises(ValueError):
        llm_client_class("nope")


def test_conversation_answer_is_read_only_prompted():
    client = _CapturingClient()
    answer = client.answer_question("Who are you?", "Candidate restaurants (1): Pala 148")
    assert answer == "I manage lunch polls."
    system, user, max_tokens = client.call
    assert "Never claim" in system
    assert "changed any state" in system
    assert "Pala 148" in user
    assert max_tokens == 300


def test_route_message_returns_typed_validated_command():
    client = _RoutingClient(
        {
            "intent": "add_to_poll",
            "mode": "execute",
            "restaurant_names": ["Pizza Rustica"],
            "cuisines": [],
            "count": None,
            "negated": False,
            "hypothetical": False,
            "ambiguous": False,
            "clarification": None,
        }
    )
    command = client.route_message(
        "put Pizza Rustica on this week's poll",
        "Candidate restaurants: Pizza Rustica [pizza]",
    )
    assert command.kind == "add_to_poll"
    assert command.queries == ["Pizza Rustica"]
    system, user, max_tokens = client.call
    assert "untrusted data" in system
    assert "Pizza Rustica [pizza]" in user
    assert max_tokens == 400


def test_build_llm_client_anthropic(tmp_path):
    # anthropic SDK is a project dependency; constructing the client makes no
    # network call.
    cfg = _cfg(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-test"})
    client = build_llm_client(cfg)
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-haiku-4-5"


def test_build_llm_client_openai(tmp_path):
    pytest.importorskip("openai")  # optional at test time
    cfg = _cfg(tmp_path, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-test"})
    client = build_llm_client(cfg)
    assert isinstance(client, OpenAIClient)
    assert client.model == "gpt-4o-mini"


# --- startup: required LLM key depends on the active provider ---


def test_startup_key_openai_selected_flags_openai_only(tmp_path):
    # provider=openai, only the (irrelevant) anthropic key is set.
    cfg = _cfg(tmp_path, {"LLM_PROVIDER": "openai", "ANTHROPIC_API_KEY": "sk-ant-x"})
    assert active_llm_api_key(cfg) is None  # anthropic key does not count
    assert missing_llm_key_env(cfg) == "OPENAI_API_KEY"


def test_startup_key_openai_present(tmp_path):
    cfg = _cfg(tmp_path, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-x"})
    assert active_llm_api_key(cfg) == "sk-openai-x"
    assert missing_llm_key_env(cfg) is None


def test_startup_key_anthropic_selected_flags_anthropic_only(tmp_path):
    # default provider=anthropic, only the (irrelevant) openai key is set.
    cfg = _cfg(tmp_path, {"OPENAI_API_KEY": "sk-openai-x"})
    assert cfg.llm_provider == "anthropic"
    assert active_llm_api_key(cfg) is None  # openai key does not count
    assert missing_llm_key_env(cfg) == "ANTHROPIC_API_KEY"


def test_startup_key_anthropic_present(tmp_path):
    cfg = _cfg(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert active_llm_api_key(cfg) == "sk-ant-x"
    assert missing_llm_key_env(cfg) is None
