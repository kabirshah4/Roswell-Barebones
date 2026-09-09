"""Which AI provider gets picked, and why.

Enrichment fires per-article across the whole watchlist, so picking the paid
provider when a free one is configured is a real cost bug, not a preference.
"""

import pytest

from backend.config import Config
from backend.main import _pick_ai_client, _pick_chat_agent
from backend.services.chat_agent import ChatAgent
from backend.services.claude_client import ClaudeClient
from backend.services.gemini_agent import GeminiAgent
from backend.services.gemini_enricher import GeminiEnricher


@pytest.fixture
def no_keys(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")


def test_explicit_gemini_is_honoured(no_keys):
    """Even with no key: the class is chosen by config, enablement by the key."""
    assert isinstance(_pick_ai_client(Config(chat_provider="gemini")), GeminiEnricher)


def test_explicit_anthropic_is_honoured(gemini_key):
    """A Gemini key present must not override an explicit anthropic setting."""
    assert isinstance(_pick_ai_client(Config(chat_provider="anthropic")), ClaudeClient)


def test_auto_prefers_gemini_when_its_key_is_present(gemini_key):
    assert isinstance(_pick_ai_client(Config(chat_provider="auto")), GeminiEnricher)


def test_auto_falls_back_to_anthropic_without_a_gemini_key(no_keys):
    assert isinstance(_pick_ai_client(Config(chat_provider="auto")), ClaudeClient)


def test_google_api_key_also_selects_gemini(monkeypatch, no_keys):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert isinstance(_pick_ai_client(Config(chat_provider="auto")), GeminiEnricher)


def test_enricher_and_chat_agree_on_provider(gemini_key):
    """Split providers would bill Anthropic for half the app by surprise."""
    cfg = Config(chat_provider="auto")
    assert isinstance(_pick_ai_client(cfg), GeminiEnricher)
    assert isinstance(_pick_chat_agent(cfg), GeminiAgent)


def test_both_fall_back_together(no_keys):
    cfg = Config(chat_provider="auto")
    assert isinstance(_pick_ai_client(cfg), ClaudeClient)
    assert isinstance(_pick_chat_agent(cfg), ChatAgent)


def test_provider_setting_is_case_and_space_insensitive(no_keys):
    assert isinstance(_pick_ai_client(Config(chat_provider="  GEMINI ")), GeminiEnricher)
