from backend.services import chat_tools
from backend.services.gemini_agent import GeminiAgent, to_function_declarations


class Part:
    """Stands in for a google.genai Part."""

    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class Call:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class Response:
    def __init__(self, parts):
        self.candidates = [type("C", (), {"content": type("X", (), {"parts": parts})()})()]


def caller_returning(*responses):
    seq = list(responses)
    calls = []

    def caller(contents, declarations, model=None):
        calls.append((contents, declarations))
        return seq.pop(0) if seq else Response([Part(text="done")])

    caller.calls = calls
    return caller


# --- schema translation -----------------------------------------------------

def test_translation_covers_every_tool():
    decls = to_function_declarations(chat_tools.TOOL_SCHEMAS)
    assert len(decls) == len(chat_tools.TOOL_SCHEMAS)
    assert {d["name"] for d in decls} == {s["name"] for s in chat_tools.TOOL_SCHEMAS}


def test_translation_renames_input_schema_to_parameters():
    decls = to_function_declarations(chat_tools.TOOL_SCHEMAS)
    for d in decls:
        assert "parameters" in d
        assert "input_schema" not in d
        assert d["parameters"]["type"] == "object"


def test_translation_preserves_descriptions():
    """The descriptions carry the pair-grades-with-stats instruction."""
    decls = {d["name"]: d for d in to_function_declarations(chat_tools.TOOL_SCHEMAS)}
    assert "get_backtest_stats" in decls["get_signals"]["description"]


def test_translation_tolerates_a_schema_without_properties():
    decls = to_function_declarations([{"name": "x", "description": "d"}])
    assert decls[0]["parameters"] == {"type": "object", "properties": {}}


# --- the loop ---------------------------------------------------------------

def test_disabled_without_key_makes_no_calls(db_path):
    agent = GeminiAgent(caller=None, _force_disabled=True)
    assert agent.enabled is False
    assert "GEMINI_API_KEY" in agent.ask("hi", [], db_path)["reply"]


def test_plain_answer_is_returned(db_path):
    agent = GeminiAgent(caller=caller_returning(Response([Part(text="AAPL is fine.")])))
    assert agent.ask("how is AAPL?", [], db_path)["reply"] == "AAPL is fine."


def test_function_call_loop_executes_a_real_tool(db_path):
    from backend.db import database

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")

    caller = caller_returning(
        Response([Part(function_call=Call("get_watchlist"))]),
        Response([Part(text="You track AAPL.")]),
    )
    out = GeminiAgent(caller=caller).ask("what do I track?", [], db_path)
    assert out["reply"] == "You track AAPL."
    assert "get_watchlist" in out["tools_used"]


def test_tool_result_is_fed_back(db_path):
    caller = caller_returning(
        Response([Part(function_call=Call("get_watchlist"))]),
        Response([Part(text="ok")]),
    )
    GeminiAgent(caller=caller).ask("q", [], db_path)
    second_contents, _ = caller.calls[1]
    assert any("function_response" in str(p) for c in second_contents for p in c.get("parts", []))


def test_unknown_tool_does_not_crash_the_loop(db_path):
    caller = caller_returning(
        Response([Part(function_call=Call("bogus"))]),
        Response([Part(text="recovered")]),
    )
    assert GeminiAgent(caller=caller).ask("q", [], db_path)["reply"] == "recovered"


def test_turn_cap_stops_an_infinite_loop(db_path):
    def always_call(contents, declarations):
        return Response([Part(function_call=Call("get_watchlist"))])

    out = GeminiAgent(caller=always_call, max_turns=3).ask("q", [], db_path)
    assert "reply" in out


def test_history_is_mapped_to_gemini_roles(db_path):
    caller = caller_returning(Response([Part(text="ok")]))
    GeminiAgent(caller=caller).ask(
        "second", [{"role": "assistant", "content": "first"}], db_path)
    contents, _ = caller.calls[0]
    assert contents[0]["role"] == "model", "assistant must map to Gemini's 'model'"


def test_empty_candidates_does_not_raise(db_path):
    class Empty:
        candidates = []

    assert "reply" in GeminiAgent(caller=lambda c, d, m=None: Empty()).ask("q", [], db_path)


# --- actionable errors, no key leakage --------------------------------------

def _raising(msg):
    def caller(contents, declarations, model=None):
        raise RuntimeError(msg)

    return GeminiAgent(caller=caller)


def test_invalid_key_is_explained(db_path):
    reply = _raising("API key not valid. Please pass a valid API key.").ask("q", [], db_path)["reply"]
    assert "GEMINI_API_KEY" in reply


def test_quota_is_explained(db_path):
    reply = _raising("429 RESOURCE_EXHAUSTED quota exceeded").ask("q", [], db_path)["reply"]
    assert "quota" in reply.lower() or "rate-limit" in reply.lower()


def test_provider_text_is_never_echoed(db_path):
    sentinel = "AQ_SENTINEL_KEY_MUST_NOT_APPEAR"
    reply = _raising(f"API key not valid: {sentinel}").ask("q", [], db_path)["reply"]
    assert sentinel not in reply


def test_key_never_reaches_logs(caplog, db_path):
    caplog.set_level("DEBUG")
    sentinel = "AQ_SENTINEL_KEY_MUST_NOT_APPEAR"
    _raising(f"boom {sentinel}").ask("q", [], db_path)
    assert sentinel not in caplog.text


# --- provider selection ------------------------------------------------------

def test_auto_prefers_gemini_when_its_key_is_present(monkeypatch, db_path):
    """Gemini has a free tier; Anthropic bills per call. Auto should prefer free."""
    from backend.config import Config
    from backend.main import _pick_chat_agent

    monkeypatch.setenv("GEMINI_API_KEY", "AQ-fake-not-real")
    agent = _pick_chat_agent(Config(db_path=db_path, chat_provider="auto"))
    assert type(agent).__name__ == "GeminiAgent"


def test_auto_falls_back_to_anthropic_without_a_gemini_key(monkeypatch, db_path):
    from backend.config import Config
    from backend.main import _pick_chat_agent

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    agent = _pick_chat_agent(Config(db_path=db_path, chat_provider="auto"))
    assert type(agent).__name__ == "ChatAgent"


def test_explicit_provider_wins_over_auto_detection(monkeypatch, db_path):
    from backend.config import Config
    from backend.main import _pick_chat_agent

    monkeypatch.setenv("GEMINI_API_KEY", "AQ-fake-not-real")
    agent = _pick_chat_agent(Config(db_path=db_path, chat_provider="anthropic"))
    assert type(agent).__name__ == "ChatAgent", "an explicit choice must not be overridden"


def test_retired_model_error_names_the_replacement(db_path):
    """The provider names a replacement model; surfacing it saves a debugging trip."""
    msg = ("404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available "
           "to new users. Please update your code to use models/gemini-3.6-flash")
    reply = _raising(msg).ask("q", [], db_path)["reply"]
    assert "gemini_model" in reply
    assert "gemini-3.6-flash" in reply, "the suggested replacement should be surfaced"


def test_overloaded_model_is_explained(db_path):
    reply = _raising("503 UNAVAILABLE. This model is currently experiencing high load").ask(
        "q", [], db_path)["reply"]
    assert "overloaded" in reply.lower()


def test_model_suggestion_extraction_cannot_echo_a_key(db_path):
    """Only the models/... fragment is echoed, never the surrounding body."""
    sentinel = "AQ.SENTINEL_KEY"
    msg = f"404 NOT_FOUND key={sentinel} use models/gemini-3.6-flash instead"
    reply = _raising(msg).ask("q", [], db_path)["reply"]
    assert sentinel not in reply
    assert "gemini-3.6-flash" in reply
