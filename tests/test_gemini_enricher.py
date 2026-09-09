import json

from backend.services.claude_client import REFUSED, Enrichment
from backend.services.gemini_enricher import GeminiEnricher


class FakeReason:
    def __init__(self, name):
        self.name = name


class FakeCandidate:
    def __init__(self, finish_reason=None):
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, text=None, finish_reason=None, candidates=None):
        self.text = text
        if candidates is None:
            candidates = [FakeCandidate(finish_reason)] if finish_reason else []
        self.candidates = candidates


def caller_returning(response):
    calls = []

    def f(article):
        calls.append(article)
        return response

    f.calls = calls
    return f


def test_disabled_makes_no_call():
    called = []
    e = GeminiEnricher(caller=lambda a: called.append(a), _force_disabled=True)
    assert e.enabled is False
    assert e.enrich("t", "s") is None
    assert called == []


def test_happy_path():
    body = json.dumps({"summary": "Earnings beat.", "sentiment": "bullish"})
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text=body)))
    assert e.enabled is True
    assert e.enrich("Apple beats", "blurb") == Enrichment("Earnings beat.", "bullish")


def test_prompt_includes_headline_and_blurb():
    body = json.dumps({"summary": "s", "sentiment": "neutral"})
    c = caller_returning(FakeResponse(text=body))
    GeminiEnricher(caller=c).enrich("HEADLINE", "BLURB")
    assert "HEADLINE" in c.calls[0] and "BLURB" in c.calls[0]


def test_unknown_sentiment_falls_back_to_neutral():
    body = json.dumps({"summary": "s", "sentiment": "extremely spicy"})
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text=body)))
    assert e.enrich("t", "s").sentiment == "neutral"


def test_sentiment_is_case_insensitive():
    body = json.dumps({"summary": "s", "sentiment": "BEARISH"})
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text=body)))
    assert e.enrich("t", "s").sentiment == "bearish"


def test_safety_block_is_refused_not_retried():
    r = FakeResponse(text="", finish_reason=FakeReason("SAFETY"))
    e = GeminiEnricher(caller=caller_returning(r))
    assert e.enrich("t", "s") is REFUSED


def test_prohibited_content_is_refused():
    r = FakeResponse(text="", finish_reason=FakeReason("PROHIBITED_CONTENT"))
    assert GeminiEnricher(caller=caller_returning(r)).enrich("t", "s") is REFUSED


def test_normal_stop_is_not_treated_as_refusal():
    body = json.dumps({"summary": "s", "sentiment": "neutral"})
    r = FakeResponse(text=body, finish_reason=FakeReason("STOP"))
    assert isinstance(GeminiEnricher(caller=caller_returning(r)).enrich("t", "s"), Enrichment)


def test_empty_text_is_transient_none():
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text="")))
    assert e.enrich("t", "s") is None


def test_malformed_json_is_none_not_a_crash():
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text="not json")))
    assert e.enrich("t", "s") is None


def test_blank_summary_is_rejected():
    body = json.dumps({"summary": "   ", "sentiment": "bullish"})
    e = GeminiEnricher(caller=caller_returning(FakeResponse(text=body)))
    assert e.enrich("t", "s") is None


def test_exception_never_escapes():
    def boom(article):
        raise RuntimeError("network")

    assert GeminiEnricher(caller=boom).enrich("t", "s") is None


def test_error_text_is_never_logged(caplog):
    """A provider error body can embed the API key."""
    def boom(article):
        raise RuntimeError("key=AQ.SECRET123 rejected")

    with caplog.at_level("DEBUG"):
        GeminiEnricher(caller=boom).enrich("t", "s")
    assert "SECRET123" not in caplog.text
