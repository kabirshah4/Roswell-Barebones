from backend.services.chat_agent import SYSTEM_PROMPT, ChatAgent


class Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class Reply:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def text_only(msg="Here is the answer."):
    def creator(**kw):
        return Reply([Block("text", text=msg)])

    creator.calls = []
    return creator


def recording(replies):
    seq = list(replies)
    calls = []

    def creator(**kw):
        calls.append(kw)
        return seq.pop(0) if seq else Reply([Block("text", text="done")])

    creator.calls = calls
    return creator


def test_disabled_without_credentials_makes_no_calls(db_path):
    agent = ChatAgent(message_creator=None, _force_disabled=True)
    assert agent.enabled is False
    assert "ANTHROPIC_API_KEY" in agent.ask("hi", [], db_path)["reply"]


def test_plain_answer_is_returned(db_path):
    agent = ChatAgent(message_creator=text_only("AAPL looks fine."))
    assert agent.ask("how is AAPL?", [], db_path)["reply"] == "AAPL looks fine."


def test_tool_use_loop_executes_and_reports(db_path):
    from backend.db import database

    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")

    creator = recording([
        Reply([Block("tool_use", id="t1", name="get_watchlist", input={})],
              stop_reason="tool_use"),
        Reply([Block("text", text="You track AAPL.")]),
    ])
    out = ChatAgent(message_creator=creator).ask("what do I track?", [], db_path)
    assert out["reply"] == "You track AAPL."
    assert "get_watchlist" in out["tools_used"]


def test_tool_result_is_fed_back_to_the_model(db_path):
    creator = recording([
        Reply([Block("tool_use", id="t1", name="get_watchlist", input={})],
              stop_reason="tool_use"),
        Reply([Block("text", text="ok")]),
    ])
    ChatAgent(message_creator=creator).ask("q", [], db_path)
    assert "tool_result" in str(creator.calls[1]["messages"])


def test_unknown_tool_does_not_crash_the_loop(db_path):
    creator = recording([
        Reply([Block("tool_use", id="t1", name="bogus", input={})],
              stop_reason="tool_use"),
        Reply([Block("text", text="recovered")]),
    ])
    assert ChatAgent(message_creator=creator).ask("q", [], db_path)["reply"] == "recovered"


def test_turn_cap_prevents_an_infinite_tool_loop(db_path):
    def always_tool(**kw):
        return Reply([Block("tool_use", id="t", name="get_watchlist", input={})],
                     stop_reason="tool_use")

    out = ChatAgent(message_creator=always_tool, max_turns=3).ask("q", [], db_path)
    assert "reply" in out


def test_exception_returns_a_message_not_a_raise(db_path):
    def boom(**kw):
        raise RuntimeError("provider down")

    assert "reply" in ChatAgent(message_creator=boom).ask("q", [], db_path)


def test_history_is_passed_through(db_path):
    creator = recording([Reply([Block("text", text="ok")])])
    ChatAgent(message_creator=creator).ask(
        "second", [{"role": "user", "content": "first"}], db_path)
    assert "first" in str(creator.calls[0]["messages"])


def test_tools_are_offered_to_the_model(db_path):
    creator = recording([Reply([Block("text", text="ok")])])
    ChatAgent(message_creator=creator).ask("q", [], db_path)
    assert len(creator.calls[0]["tools"]) >= 8


def test_system_prompt_forbids_inventing_levels():
    lowered = SYSTEM_PROMPT.lower()
    assert "never" in lowered
    assert "invent" in lowered or "originate" in lowered


def test_system_prompt_requires_pairing_grades_with_measured_stats():
    assert "get_backtest_stats" in SYSTEM_PROMPT


def test_system_prompt_states_it_is_not_advice():
    assert "not financial advice" in SYSTEM_PROMPT.lower()


def test_api_key_never_reaches_logs(caplog, db_path):
    caplog.set_level("DEBUG")
    sentinel = "SENTINEL_CHAT_KEY_MUST_NOT_LEAK"

    def boom(**kw):
        raise RuntimeError(f"auth failed for {sentinel}")

    ChatAgent(message_creator=boom).ask("q", [], db_path)
    assert sentinel not in caplog.text


# --- actionable provider errors --------------------------------------------
# A user should not have to read server logs to learn their account is out of
# credit. These map known failures to our OWN wording - the provider message is
# never echoed, since an error body can carry the API key.

def _agent_raising(message):
    def boom(**kw):
        raise RuntimeError(message)

    return ChatAgent(message_creator=boom)


def test_out_of_credit_is_explained(db_path):
    reply = _agent_raising(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API."
    ).ask("q", [], db_path)["reply"]
    assert "out of credit" in reply.lower()
    assert "console.anthropic.com" in reply


def test_bad_key_is_explained(db_path):
    reply = _agent_raising("invalid x-api-key").ask("q", [], db_path)["reply"]
    assert "ANTHROPIC_API_KEY" in reply


def test_rate_limit_is_explained(db_path):
    reply = _agent_raising("rate limit exceeded").ask("q", [], db_path)["reply"]
    assert "rate-limiting" in reply.lower()


def test_unknown_failure_still_names_the_type(db_path):
    reply = _agent_raising("something bizarre").ask("q", [], db_path)["reply"]
    assert "RuntimeError" in reply


def test_provider_text_is_never_echoed_back(db_path):
    """An error body can embed the key; only our own strings may reach the user."""
    sentinel = "sk-ant-SENTINEL_MUST_NOT_APPEAR"
    reply = _agent_raising(
        f"credit balance is too low (key {sentinel})"
    ).ask("q", [], db_path)["reply"]
    assert sentinel not in reply


# --- the prompt must permit grounded projection ------------------------------

def test_the_prompt_allows_forward_looking_answers():
    """The user asked for estimates; refusing every one is a non-answer."""
    from backend.services.chat_agent import SYSTEM_PROMPT

    assert "get_price_outlook" in SYSTEM_PROMPT


def test_the_prompt_still_forbids_inventing_levels():
    """Projection is computed. Entry/stop/target are still engine-only."""
    from backend.services.chat_agent import SYSTEM_PROMPT

    assert "NEVER invent or originate a price level" in SYSTEM_PROMPT


def test_the_prompt_requires_the_range_not_just_a_number():
    from backend.services.chat_agent import SYSTEM_PROMPT

    assert "RANGE" in SYSTEM_PROMPT


def test_the_prompt_blocks_estimating_from_training_data():
    """The model's weights have no idea what this stock did last week."""
    from backend.services.chat_agent import SYSTEM_PROMPT

    assert "training data" in SYSTEM_PROMPT.lower()


def test_the_prompt_asks_for_an_actual_view():
    from backend.services.chat_agent import SYSTEM_PROMPT

    assert "commit to a view" in SYSTEM_PROMPT
