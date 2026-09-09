"""The chat headroom must actually reach the provider call.

A ceiling that lives only in config, or defaults that main.py forgets to pass
through, looks correct and behaves exactly like the old tight limit.
"""

import pytest

from backend.config import Config
from backend.main import _pick_chat_agent
from backend.services.chat_agent import ChatAgent
from backend.services.gemini_agent import GeminiAgent


class Part:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class Call:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class Response:
    def __init__(self, parts):
        self.candidates = [type("C", (), {"content": type("D", (), {"parts": parts})()})()]


def tool_caller(rounds):
    """A caller that keeps asking for a tool, so the turn ceiling is what stops it."""
    seen = []

    def f(contents, declarations, model=None):
        seen.append(1)
        if len(seen) <= rounds:
            return Response([Part(function_call=Call("get_watchlist"))])
        return Response([Part(text="done")])

    f.seen = seen
    return f


# --- turn ceiling -----------------------------------------------------------

def test_gemini_defaults_to_the_wide_turn_ceiling():
    assert GeminiAgent(caller=lambda *a: None)._max_turns == 24


def test_anthropic_defaults_to_the_wide_turn_ceiling():
    assert ChatAgent(message_creator=lambda **k: None)._max_turns == 24


def test_a_long_tool_chain_completes_within_the_default_ceiling(db_path):
    """Twelve lookups is an ordinary "what should I buy" answer, not an abuse."""
    caller = tool_caller(12)
    out = GeminiAgent(caller=caller).ask("what should I buy", [], db_path)
    assert out["reply"] == "done"
    assert len(out["tools_used"]) == 12


def test_the_ceiling_still_stops_a_runaway(db_path):
    caller = tool_caller(1000)
    out = GeminiAgent(caller=caller, max_turns=5).ask("q", [], db_path)
    assert "5 lookup steps" in out["reply"]
    assert len(caller.seen) == 5


def test_the_runaway_message_names_the_real_ceiling(db_path):
    """A hardcoded number would go stale the moment the ceiling is tuned."""
    out = GeminiAgent(caller=tool_caller(1000), max_turns=7).ask("q", [], db_path)
    assert "7 lookup steps" in out["reply"]


# --- token ceiling ----------------------------------------------------------

def test_anthropic_sends_the_configured_max_tokens(db_path):
    seen = {}

    def creator(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    ChatAgent(message_creator=creator, max_tokens=8192).ask("q", [], db_path)
    assert seen["max_tokens"] == 8192


def test_anthropic_max_tokens_is_not_hardcoded(db_path):
    seen = {}

    def creator(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    ChatAgent(message_creator=creator, max_tokens=1234).ask("q", [], db_path)
    assert seen["max_tokens"] == 1234


# --- main.py wiring ---------------------------------------------------------

@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")


@pytest.fixture
def no_keys(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_config_ceilings_reach_the_gemini_agent(gemini_key):
    agent = _pick_chat_agent(Config(chat_provider="gemini", chat_max_turns=40))
    assert agent._max_turns == 40


def test_config_ceilings_reach_the_anthropic_agent(no_keys):
    agent = _pick_chat_agent(Config(chat_provider="anthropic", chat_max_turns=40))
    assert agent._max_turns == 40


def test_the_auto_path_also_carries_the_ceilings(gemini_key):
    """The fallback branch is easy to wire up and then forget."""
    agent = _pick_chat_agent(Config(chat_provider="auto", chat_max_turns=33))
    assert isinstance(agent, GeminiAgent)
    assert agent._max_turns == 33


def test_the_auto_fallback_carries_them_too(no_keys):
    agent = _pick_chat_agent(Config(chat_provider="auto", chat_max_turns=33))
    assert isinstance(agent, ChatAgent)
    assert agent._max_turns == 33


def test_the_retry_budget_reaches_the_gemini_agent(gemini_key):
    """Without it the agent silently falls back to its own default."""
    agent = _pick_chat_agent(
        Config(chat_provider="gemini", chat_retry_budget_seconds=175.0)
    )
    assert agent._retry_budget == 175.0


def test_the_auto_path_carries_the_retry_budget(gemini_key):
    agent = _pick_chat_agent(
        Config(chat_provider="auto", chat_retry_budget_seconds=175.0)
    )
    assert isinstance(agent, GeminiAgent)
    assert agent._retry_budget == 175.0


def test_the_shipped_retry_budget_covers_several_waits():
    """One 429 wait is ~21s; a budget under that retries nothing at all."""
    assert Config().chat_retry_budget_seconds >= 60.0


def test_the_shipped_defaults_are_generous():
    cfg = Config()
    assert cfg.chat_max_turns >= 20
    assert cfg.chat_max_tokens >= 4096
    assert cfg.chat_history_turns >= 40
