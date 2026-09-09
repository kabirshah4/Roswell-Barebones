from dataclasses import dataclass

from backend.services.claude_client import REFUSED, ClaudeClient, Enrichment


@dataclass
class FakeParsed:
    summary: str
    sentiment: str


class FakeResponse:
    def __init__(self, parsed=None, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason


def creator_returning(response):
    calls = []

    def creator(**kwargs):
        calls.append(kwargs)
        return response

    creator.calls = calls
    return creator


def creator_raising(exc):
    def creator(**kwargs):
        raise exc

    return creator


def test_enrich_returns_parsed_result():
    creator = creator_returning(FakeResponse(FakeParsed("Apple fell on a downgrade.", "bearish")))
    client = ClaudeClient(message_creator=creator)
    result = client.enrich("Apple downgraded", "Jefferies cut Apple to underperform.")
    assert result == Enrichment(summary="Apple fell on a downgrade.", sentiment="bearish")


def test_enabled_is_true_when_creator_injected():
    client = ClaudeClient(message_creator=creator_returning(FakeResponse()))
    assert client.enabled is True


def test_disabled_client_makes_no_calls_and_returns_none():
    """With no credentials the client must be inert, not merely failing."""
    client = ClaudeClient(message_creator=None, _force_disabled=True)
    assert client.enabled is False
    assert client.enrich("t", "s") is None


def test_refusal_returns_sentinel_without_touching_content():
    """A refusal is HTTP 200 with empty content; reading it would crash.

    It must return the dedicated ``REFUSED`` sentinel, not ``None`` -- the
    poller treats a refusal as deterministic and fails it immediately rather
    than spending its generic 3-attempt retry budget on it (design doc §8).
    """
    creator = creator_returning(FakeResponse(parsed=None, stop_reason="refusal"))
    client = ClaudeClient(message_creator=creator)
    assert client.enrich("t", "s") is REFUSED


def test_refusal_guard_fires_even_with_a_populated_parsed_output():
    """The refusal check must run before the parsed-output check.

    A response with `parsed=None` alongside `stop_reason="refusal"` cannot
    distinguish "the refusal guard fired" from "the guard is absent and we
    merely fell through to the missing-parsed-output check" -- both paths
    return non-Enrichment values either way. Giving the fake response a
    *truthy* parsed_output forces the two code paths to diverge: with the
    guard, this must return REFUSED; without it, `parsed` is non-None and it
    would return an Enrichment instead.
    """
    creator = creator_returning(
        FakeResponse(parsed=FakeParsed("Should never be read.", "bullish"), stop_reason="refusal")
    )
    client = ClaudeClient(message_creator=creator)
    assert client.enrich("t", "s") is REFUSED


def test_exception_returns_none():
    client = ClaudeClient(message_creator=creator_raising(RuntimeError("boom")))
    assert client.enrich("t", "s") is None


def test_missing_parsed_output_returns_none():
    client = ClaudeClient(message_creator=creator_returning(FakeResponse(parsed=None)))
    assert client.enrich("t", "s") is None


def test_invalid_sentiment_is_coerced_to_neutral():
    creator = creator_returning(FakeResponse(FakeParsed("Something.", "VERY BULLISH!!")))
    client = ClaudeClient(message_creator=creator)
    assert client.enrich("t", "s").sentiment == "neutral"


def test_sentiment_is_normalised_to_lowercase():
    creator = creator_returning(FakeResponse(FakeParsed("Something.", "Bullish")))
    client = ClaudeClient(message_creator=creator)
    assert client.enrich("t", "s").sentiment == "bullish"


def test_model_is_passed_through():
    creator = creator_returning(FakeResponse(FakeParsed("s", "neutral")))
    client = ClaudeClient(message_creator=creator, model="claude-haiku-4-5")
    client.enrich("t", "s")
    assert creator.calls[0]["model"] == "claude-haiku-4-5"


def test_article_text_reaches_the_prompt():
    creator = creator_returning(FakeResponse(FakeParsed("s", "neutral")))
    client = ClaudeClient(message_creator=creator)
    client.enrich("Apple downgraded", "Jefferies cut the rating.")
    sent = str(creator.calls[0]["messages"])
    assert "Apple downgraded" in sent
    assert "Jefferies cut the rating." in sent
