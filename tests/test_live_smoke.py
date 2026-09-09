"""Opt-in. Run with: uv run pytest -m live"""

import pytest

from backend.services.fred_client import FredClient
from backend.services.yfinance_client import YFinanceClient


@pytest.mark.live
def test_real_yahoo_still_returns_a_quote():
    """Early warning that the unofficial yfinance dependency has broken upstream."""
    quotes = YFinanceClient().fetch_quotes(["AAPL"])
    assert "AAPL" in quotes, "yfinance returned nothing for AAPL — upstream likely broke"
    assert quotes["AAPL"].price > 0


@pytest.mark.live
def test_real_yahoo_still_returns_intraday_points():
    assert len(YFinanceClient().fetch_intraday("AAPL")) > 0


@pytest.mark.live
def test_real_yahoo_returns_news():
    articles = YFinanceClient().fetch_news("AAPL")
    assert len(articles) > 0, "yfinance returned no news for AAPL — upstream likely changed"
    assert articles[0].title


@pytest.mark.live
def test_real_yahoo_returns_fundamentals():
    f = YFinanceClient().fetch_fundamentals("AAPL")
    assert f is not None
    assert f.market_cap and f.market_cap > 0


@pytest.mark.live
def test_real_fred_returns_upcoming_events():
    """Skipped when no FRED key is configured — the app is designed to work without one."""
    client = FredClient()
    if not client.enabled:
        pytest.skip("no FRED_API_KEY configured")
    events = client.fetch_upcoming(days=45)
    assert len(events) > 0, "FRED returned no curated events — check the IMPACT map names"
    assert all(e.impact in ("high", "medium", "low") for e in events)
