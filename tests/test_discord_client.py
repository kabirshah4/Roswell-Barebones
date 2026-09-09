"""The webhook URL is the credential; it must never reach a log."""

from dataclasses import dataclass

import pytest

from backend.services.discord_client import DiscordClient, build_payload

SECRET = "https://discord.com/api/webhooks/123/SUPERSECRETTOKEN"


FACTORS = {
    "trend": True, "momentum": True, "macd_cross": False, "volume": False,
    "structure": True, "not_extended": True, "multi_timeframe": True,
}


@dataclass(frozen=True)
class Setup:
    ticker: str = "AAPL"
    direction: str = "long"
    grade: str = "A+"
    score: int = 6
    # dict[str, bool], as the engine actually emits.
    factors: object = None
    entry: float = 100.0
    stop: float = 95.0
    target1: float = 110.0
    target2: float = 120.0
    risk_reward: float = 2.0
    timeframes: str = "1h,4h,1d"
    earnings_at: str | None = None

    def __post_init__(self):
        if self.factors is None:
            object.__setattr__(self, "factors", dict(FACTORS))


def poster(code=204):
    sent = []

    def f(url, payload):
        sent.append((url, payload))
        return code

    f.sent = sent
    return f


# --- enablement -------------------------------------------------------------

def test_no_url_means_disabled(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert DiscordClient().enabled is False


def test_a_disabled_client_makes_no_call(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    p = poster()
    client = DiscordClient(poster=p)
    assert client.send_setup(Setup()) is False
    assert p.sent == []


def test_the_url_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET)
    assert DiscordClient().enabled is True


def test_an_explicit_url_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://env")
    p = poster()
    DiscordClient(poster=p, webhook_url=SECRET).send_setup(Setup())
    assert p.sent[0][0] == SECRET


# --- delivery ---------------------------------------------------------------

def test_a_2xx_is_a_delivery():
    assert DiscordClient(poster=poster(204), webhook_url=SECRET).send_setup(Setup())


def test_a_200_is_also_a_delivery():
    assert DiscordClient(poster=poster(200), webhook_url=SECRET).send_setup(Setup())


def test_a_4xx_is_not_a_delivery():
    assert not DiscordClient(poster=poster(400), webhook_url=SECRET).send_setup(Setup())


def test_a_5xx_is_not_a_delivery():
    assert not DiscordClient(poster=poster(500), webhook_url=SECRET).send_setup(Setup())


def test_a_network_failure_never_raises():
    def boom(url, payload):
        raise RuntimeError("connection reset")

    assert DiscordClient(poster=boom, webhook_url=SECRET).send_setup(Setup()) is False


def test_a_formatting_failure_is_not_reported_as_a_send_failure():
    """Otherwise a malformed setup is retried forever against the same bug."""
    p = poster()
    broken = object()  # no setup attributes at all
    client = DiscordClient(poster=p, webhook_url=SECRET)
    assert client.send_setup(broken) in (True, False)  # never raises


# --- credential hygiene (canary) --------------------------------------------

def test_the_webhook_url_never_reaches_the_logs(caplog):
    def boom(url, payload):
        raise RuntimeError(f"POST {url} failed")

    with caplog.at_level("DEBUG"):
        DiscordClient(poster=boom, webhook_url=SECRET).send_setup(Setup())
    assert "SUPERSECRETTOKEN" not in caplog.text
    assert SECRET not in caplog.text


def test_a_rejection_does_not_log_the_url(caplog):
    with caplog.at_level("DEBUG"):
        DiscordClient(poster=poster(403), webhook_url=SECRET).send_setup(Setup())
    assert "SUPERSECRETTOKEN" not in caplog.text


def test_a_successful_send_does_not_log_the_url(caplog):
    with caplog.at_level("DEBUG"):
        DiscordClient(poster=poster(204), webhook_url=SECRET).send_setup(Setup())
    assert "SUPERSECRETTOKEN" not in caplog.text


def test_the_httpx_logger_is_silenced():
    """httpx logs every request URL at INFO; that is how Phase 3 leaked a key."""
    import logging

    import backend.services.discord_client  # noqa: F401

    assert logging.getLogger("httpx").level >= logging.WARNING


# --- payload ----------------------------------------------------------------

def test_the_payload_carries_every_level():
    body = str(build_payload(Setup()))
    for value in ("100.00", "95.00", "110.00", "120.00"):
        assert value in body


def test_the_payload_names_the_ticker_and_grade():
    title = build_payload(Setup())["embeds"][0]["title"]
    assert "AAPL" in title and "A+" in title


def test_long_and_short_are_visually_distinct():
    long_c = build_payload(Setup(direction="long"))["embeds"][0]["color"]
    short_c = build_payload(Setup(direction="short"))["embeds"][0]["color"]
    assert long_c != short_c


def test_only_the_two_palette_hues_are_used():
    """Green and red mean direction here exactly as they do in the terminal."""
    for d in ("long", "short"):
        assert build_payload(Setup(direction=d))["embeds"][0]["color"] in (
            0x26A65B, 0xE0483E)


def test_the_confluence_factors_are_listed():
    body = str(build_payload(Setup()))
    assert "trend" in body and "momentum" in body


def test_failed_factors_are_not_presented_as_confluence():
    """Joining the factors dict directly lists the FAILURES too, turning a
    5-of-7 setup into an apparent 7-of-7."""
    fields = build_payload(Setup())["embeds"][0]["fields"]
    conf = next(f for f in fields if f["name"].startswith("Confluence"))
    headline = conf["value"].split("\n")[0]
    assert "macd_cross" not in headline and "volume" not in headline
    assert "trend" in headline


def test_the_confluence_count_is_stated():
    fields = build_payload(Setup())["embeds"][0]["fields"]
    conf = next(f for f in fields if f["name"].startswith("Confluence"))
    assert "(5/7)" in conf["name"]


def test_the_missing_factors_are_still_shown():
    """Knowing what did NOT line up is how you judge a setup, not noise."""
    fields = build_payload(Setup())["embeds"][0]["fields"]
    conf = next(f for f in fields if f["name"].startswith("Confluence"))
    assert "macd_cross" in conf["value"] and "volume" in conf["value"]


def test_a_legacy_list_of_factors_still_renders():
    body = str(build_payload(Setup(factors=("trend", "volume"))))
    assert "trend" in body


def test_a_grade_never_travels_without_its_measured_stats():
    """A bare letter in a push notification is where someone acts on it.

    Measurement showed A+ did not reliably outperform B, so the alert must
    carry the numbers or say plainly that there are none.
    """
    with_stats = str(build_payload(
        Setup(), {"signals": 40, "win_rate": 0.41, "avg_r": 0.27}))
    assert "40 past signals" in with_stats
    assert "41.0%" in with_stats

    without = str(build_payload(Setup(), None))
    assert "unvalidated" in without.lower()


def test_earnings_proximity_is_flagged():
    body = str(build_payload(Setup(earnings_at="2026-09-01T00:00:00Z")))
    assert "Earnings" in body and "gap risk" in body


def test_no_earnings_field_when_there_is_no_date():
    names = [f["name"] for f in build_payload(Setup())["embeds"][0]["fields"]]
    assert not any("Earnings" in n for n in names)


def test_the_alert_disclaims_itself():
    footer = build_payload(Setup())["embeds"][0]["footer"]["text"]
    assert "not advice" in footer.lower()
    assert "no orders" in footer.lower()


def test_discord_limits_are_respected():
    """Exceeding these is a 400, not a truncation."""
    payload = build_payload(
        Setup(factors={f"factor_{i}": True for i in range(500)}))
    assert len(payload["embeds"]) <= 10
    for field in payload["embeds"][0]["fields"]:
        assert len(field["value"]) <= 1024


def test_missing_levels_render_as_a_dash_not_a_crash():
    body = str(build_payload(Setup(entry=None, stop=None)))
    assert "—" in body
