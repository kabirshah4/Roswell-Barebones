"""Waiting out Gemini's per-minute quota instead of failing the answer.

The free tier caps requests per MINUTE, and a tool-use answer spends one
request per lookup round. Without this, an ordinary multi-ticker question
fails halfway through with nothing to show for the calls it already made.
"""

from backend.services.gemini_agent import (
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    GeminiAgent,
    _is_daily_quota,
    _is_overloaded,
    _is_rate_limit,
    _retry_delay,
)

FIRST, SECOND, THIRD = DEFAULT_MODEL, *FALLBACK_MODELS


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
        self.candidates = [
            type("C", (), {"content": type("D", (), {"parts": parts})()})()
        ]


ANSWER = Response([Part(text="done")])

# Shaped like a real google.genai 429 body, minus everything identifying.
QUOTA_ERR = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'status': "
    "'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/"
    "google.rpc.RetryInfo', 'retryDelay': '21s'}]}}"
)


class Sleeper:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def flaky(failures, error=QUOTA_ERR):
    """Raises a rate-limit error `failures` times, then answers."""
    calls = []

    def f(contents, declarations, model=None):
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError(error)
        return ANSWER

    f.calls = calls
    return f


# --- detection --------------------------------------------------------------

def test_resource_exhausted_is_a_rate_limit():
    assert _is_rate_limit(RuntimeError(QUOTA_ERR)) is True


def test_an_ordinary_error_is_not_a_rate_limit():
    assert _is_rate_limit(RuntimeError("400 INVALID_ARGUMENT")) is False


def test_the_retry_delay_is_read_from_the_error():
    assert _retry_delay(RuntimeError(QUOTA_ERR)) == 21.0


def test_no_delay_field_means_no_retry():
    assert _retry_delay(RuntimeError("429 RESOURCE_EXHAUSTED")) is None


def test_an_absurd_delay_is_clamped():
    """A multi-hour wait is a daily cap, not something to sleep off in-request."""
    err = "429 RESOURCE_EXHAUSTED 'retryDelay': '86400s'"
    assert _retry_delay(RuntimeError(err)) <= 65.0


# --- retry behaviour --------------------------------------------------------

def test_a_rate_limited_call_is_retried_and_succeeds(db_path):
    sleeper = Sleeper()
    caller = flaky(1)
    out = GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert out["reply"] == "done"
    assert sleeper.waits == [21.0]
    assert len(caller.calls) == 2


def test_it_waits_exactly_as_long_as_the_api_asked(db_path):
    sleeper = Sleeper()
    err = QUOTA_ERR.replace("'21s'", "'7s'")
    GeminiAgent(caller=flaky(1, err), sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == [7.0]


def test_repeated_limits_are_waited_out_within_budget(db_path):
    sleeper = Sleeper()
    out = GeminiAgent(caller=flaky(3), sleep=sleeper, retry_budget_seconds=90.0).ask(
        "q", [], db_path)
    assert out["reply"] == "done"
    assert sleeper.waits == [21.0, 21.0, 21.0]


def test_the_budget_is_shared_across_the_whole_answer(db_path):
    """One long question must not spend the full wait once per lookup round."""
    sleeper = Sleeper()
    out = GeminiAgent(caller=flaky(10), sleep=sleeper, retry_budget_seconds=50.0).ask(
        "q", [], db_path)
    assert sum(sleeper.waits) <= 50.0
    assert "rate-limiting" in out["reply"] or "quota" in out["reply"].lower()


def test_it_gives_up_when_the_wait_exceeds_the_budget(db_path):
    sleeper = Sleeper()
    out = GeminiAgent(caller=flaky(5), sleep=sleeper, retry_budget_seconds=10.0).ask(
        "q", [], db_path)
    assert sleeper.waits == []
    assert "quota" in out["reply"].lower() or "rate-limiting" in out["reply"]


def test_a_non_rate_limit_error_is_not_retried(db_path):
    sleeper = Sleeper()
    caller = flaky(1, "400 INVALID_ARGUMENT")
    GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == []
    assert len(caller.calls) == 1


OVERLOAD_ERR = (
    "503 UNAVAILABLE. {'error': {'code': 503, 'status': 'UNAVAILABLE', "
    "'details': [{'retryDelay': '5s'}]}}"
)


def test_an_overloaded_model_is_switched_away_from_not_slept_on(db_path):
    """Measured: gemini-flash-latest 503'd 11 of 12 requests. Waiting it out
    would stall the answer; another model answers immediately."""
    sleeper = Sleeper()
    caller = flaky(1, OVERLOAD_ERR)
    out = GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert out["reply"] == "done"
    assert sleeper.waits == []


def test_a_plain_client_error_is_never_retried_or_switched(db_path):
    """The recovery paths exist for quota and overload, not retry-on-anything."""
    sleeper = Sleeper()
    caller = flaky(1, "400 INVALID_ARGUMENT 'retryDelay': '5s'")
    GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == []
    assert len(caller.calls) == 1


def test_overload_and_daily_quota_are_told_apart():
    assert _is_overloaded(RuntimeError(OVERLOAD_ERR)) is True
    assert _is_overloaded(RuntimeError(DAILY_ERR)) is False


def test_a_429_with_no_delay_is_not_retried_blindly(db_path):
    """Guessing a wait would hang the request for an unknown length of time."""
    sleeper = Sleeper()
    caller = flaky(1, "429 RESOURCE_EXHAUSTED")
    GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == []
    assert len(caller.calls) == 1


def test_the_error_body_is_never_logged(caplog, db_path):
    sleeper = Sleeper()
    err = QUOTA_ERR + " key=AQ.SECRET123"
    with caplog.at_level("DEBUG"):
        GeminiAgent(caller=flaky(1, err), sleep=sleeper).ask("q", [], db_path)
    assert "SECRET123" not in caplog.text


# --- daily quota: change model, do not wait ---------------------------------
#
# The free-tier quota is "GenerateRequestsPerDayPerProjectPerModel" -- charged
# per MODEL. gemini-3.5-flash allows 20 requests a day, and one tool-use answer
# can spend a dozen. Falling back across models multiplies the usable budget.
# The API still returns a ~21s retryDelay against a daily cap, so waiting on it
# burns the request clock for an allowance that does not return until tomorrow.

DAILY_ERR = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': ["
    "{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
    "'quotaValue': '20'}, {'retryDelay': '21s'}]}}"
)
MINUTE_ERR = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': ["
    "{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}, "
    "{'retryDelay': '21s'}]}}"
)


def model_tracking_caller(dead_models):
    """Answers on any model not in `dead_models`; daily-quota errors otherwise."""
    seen = []

    def f(contents, declarations, model=None):
        seen.append(model)
        if model in dead_models:
            raise RuntimeError(DAILY_ERR)
        return ANSWER

    f.seen = seen
    return f


def test_a_daily_cap_is_classified_as_daily():
    assert _is_daily_quota(RuntimeError(DAILY_ERR)) is True


def test_a_per_minute_cap_is_not_classified_as_daily():
    assert _is_daily_quota(RuntimeError(MINUTE_ERR)) is False


def test_a_daily_cap_falls_back_to_the_next_model(db_path):
    caller = model_tracking_caller({FIRST})
    sleeper = Sleeper()
    out = GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert out["reply"] == "done"
    assert caller.seen == [FIRST, SECOND]


def test_a_daily_cap_never_sleeps(db_path):
    """The allowance returns tomorrow; sleeping 21s just wastes the request."""
    sleeper = Sleeper()
    GeminiAgent(caller=model_tracking_caller({FIRST}),
                sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == []


def test_it_walks_the_whole_chain(db_path):
    caller = model_tracking_caller({FIRST, SECOND})
    out = GeminiAgent(caller=caller, sleep=Sleeper()).ask("q", [], db_path)
    assert out["reply"] == "done"
    assert caller.seen[-1] == THIRD


def test_the_exhausted_model_is_not_retried_on_later_rounds(db_path):
    """Re-trying a dead model each round would spend a call to learn nothing."""
    calls = []

    def caller(contents, declarations, model=None):
        calls.append(model)
        if model == FIRST:
            raise RuntimeError(DAILY_ERR)
        if len([c for c in calls if c != FIRST]) <= 3:
            return Response([Part(function_call=Call("get_watchlist"))])
        return ANSWER

    GeminiAgent(caller=caller, sleep=Sleeper()).ask("q", [], db_path)
    assert calls.count(FIRST) == 1


def test_the_whole_chain_exhausted_reports_it_plainly(db_path):
    caller = model_tracking_caller({FIRST, SECOND, THIRD})
    out = GeminiAgent(caller=caller, sleep=Sleeper()).ask("q", [], db_path)
    assert "daily" in out["reply"].lower()
    assert "anthropic" in out["reply"].lower()


def test_the_primary_model_is_never_also_a_fallback(db_path):
    """Its allowance is exactly what ran out; re-trying it wastes a call."""
    agent = GeminiAgent(caller=lambda *a: ANSWER, model=SECOND)
    assert SECOND not in agent._fallbacks


def test_a_per_minute_cap_still_waits_rather_than_switching(db_path):
    """Switching model on a per-minute blip would abandon a working model."""
    calls = []

    def caller(contents, declarations, model=None):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError(MINUTE_ERR)
        return ANSWER

    sleeper = Sleeper()
    GeminiAgent(caller=caller, sleep=sleeper).ask("q", [], db_path)
    assert sleeper.waits == [21.0]
    assert calls == [FIRST, FIRST]


def test_fallbacks_are_configurable(db_path):
    caller = model_tracking_caller({FIRST})
    GeminiAgent(caller=caller, sleep=Sleeper(),
                fallback_models=("my-model",)).ask("q", [], db_path)
    assert caller.seen == [FIRST, "my-model"]


def test_the_shipped_chain_has_more_than_one_fallback():
    """Each model carries its own daily allowance, so the chain is the budget."""
    assert len(FALLBACK_MODELS) >= 2


def test_the_primary_is_the_model_measured_to_be_reliable():
    """Measured against a live key: flash-lite 12/12, flash-latest 1/12 (503s).

    Pinned deliberately -- picking the higher-tier model reads as an upgrade
    and silently makes the chat unusable.
    """
    assert DEFAULT_MODEL == "gemini-flash-lite-latest"


def test_no_model_appears_twice_in_the_chain():
    chain = [DEFAULT_MODEL, *FALLBACK_MODELS]
    assert len(chain) == len(set(chain))
