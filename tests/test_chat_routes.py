from fastapi.testclient import TestClient

from backend.config import Config
from backend.main import create_app


class ExplodingClient:
    def fetch_quotes(self, tickers):
        raise AssertionError("routes must never fetch")

    def fetch_intraday(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_news(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_fundamentals(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_bars(self, ticker, period, interval):
        raise AssertionError("routes must never fetch")

    def fetch_earnings_dates(self, ticker, limit=8):
        raise AssertionError("routes must never fetch")

    def check_ticker(self, ticker):
        return "ok"

    def validate_ticker(self, ticker):
        return True


class FakeAgent:
    enabled = True

    def __init__(self, reply="hello"):
        self._reply = reply
        self.calls = []

    def ask(self, message, history, db_path):
        self.calls.append((message, history))
        return {"reply": self._reply, "tools_used": ["get_prices"]}


def make_client(db_path, agent=None, **cfg_kwargs):
    app = create_app(
        cfg=Config(db_path=db_path, **cfg_kwargs), client=ExplodingClient(),
        ai_client=None, fred_client=None,
        chat_agent=agent if agent is not None else FakeAgent(),
        start_poller=False,
    )
    return TestClient(app)


def test_chat_returns_a_reply(db_path):
    with make_client(db_path) as c:
        body = c.post("/api/chat", json={"message": "hi", "history": []}).json()
    assert body["reply"] == "hello"
    assert "get_prices" in body["tools_used"]


def test_chat_passes_history_through(db_path):
    agent = FakeAgent()
    with make_client(db_path, agent) as c:
        c.post("/api/chat", json={
            "message": "second",
            "history": [{"role": "user", "content": "first"}]})
    _, history = agent.calls[0]
    assert history[0]["content"] == "first"


def test_history_is_trimmed_so_the_prompt_cannot_grow_without_bound(db_path):
    agent = FakeAgent()
    long_history = [{"role": "user", "content": str(i)} for i in range(500)]
    with make_client(db_path, agent, chat_history_turns=60) as c:
        c.post("/api/chat", json={"message": "q", "history": long_history})
    _, history = agent.calls[0]
    assert len(history) == 60


def test_the_trim_keeps_the_most_recent_turns(db_path):
    """Dropping the newest turns would make the chat forget the live thread."""
    agent = FakeAgent()
    long_history = [{"role": "user", "content": str(i)} for i in range(100)]
    with make_client(db_path, agent, chat_history_turns=10) as c:
        c.post("/api/chat", json={"message": "q", "history": long_history})
    _, history = agent.calls[0]
    assert [h["content"] for h in history] == [str(i) for i in range(90, 100)]


def test_the_history_ceiling_comes_from_config(db_path):
    """Raising the ceiling must actually widen what reaches the agent."""
    agent = FakeAgent()
    long_history = [{"role": "user", "content": str(i)} for i in range(500)]
    with make_client(db_path, agent, chat_history_turns=200) as c:
        c.post("/api/chat", json={"message": "q", "history": long_history})
    _, history = agent.calls[0]
    assert len(history) == 200


def test_a_short_history_is_passed_through_whole(db_path):
    agent = FakeAgent()
    with make_client(db_path, agent) as c:
        c.post("/api/chat", json={"message": "q",
                                  "history": [{"role": "user", "content": "hi"}]})
    _, history = agent.calls[0]
    assert len(history) == 1


def test_blank_message_rejected_with_422(db_path):
    with make_client(db_path) as c:
        assert c.post("/api/chat", json={"message": "   ", "history": []}).status_code == 422


def test_disabled_agent_still_answers_with_guidance(db_path):
    class Off:
        enabled = False

        def ask(self, message, history, db_path):
            return {"reply": "Chat is off. Set ANTHROPIC_API_KEY", "tools_used": []}

    with make_client(db_path, Off()) as c:
        body = c.post("/api/chat", json={"message": "hi", "history": []}).json()
    assert "ANTHROPIC_API_KEY" in body["reply"]


def test_health_reports_chat_enabled(db_path):
    with make_client(db_path) as c:
        assert c.get("/api/health").json()["chat_enabled"] is True


def test_health_reports_chat_disabled(db_path):
    class Off:
        enabled = False

        def ask(self, message, history, db_path):
            return {"reply": "off", "tools_used": []}

    with make_client(db_path, Off()) as c:
        assert c.get("/api/health").json()["chat_enabled"] is False
