import pandas as pd
import pytest

from backend.services.yfinance_client import (
    RATE_LIMIT_ERRORS,
    TICKER_MISSING_ERRORS,
    Fundamentals,
    NewsArticle,
    Quote,
    YFinanceClient,
)

# The real types check_ticker() catches, re-exported by the client module so
# tests can raise them without importing yfinance directly (only
# backend/services/yfinance_client.py is allowed to do that).
_TickerMissingError = TICKER_MISSING_ERRORS[0]
_RateLimitError = RATE_LIMIT_ERRORS[0]


class FakeFastInfo:
    """Mimics the yfinance 1.x FastInfo attribute surface."""

    def __init__(self, last_price, previous_close, last_volume, currency):
        self.last_price = last_price
        self.previous_close = previous_close
        self.last_volume = last_volume
        self.currency = currency


class _Series:
    """Stands in for a pandas Series of closes."""

    def __init__(self, values):
        self._values = values

    def dropna(self):
        return self

    def tolist(self):
        return self._values


class _Frame:
    """Stands in for a pandas DataFrame. __getitem__ must live on the class:
    Python resolves dunder methods on the type, not the instance."""

    def __init__(self, closes):
        self._closes = closes
        self.empty = not closes

    def __getitem__(self, key):
        assert key == "Close", f"unexpected column {key!r}"
        return _Series(self._closes)


class FakeTicker:
    def __init__(self, fast_info=None, closes=None, error=None, history_error=None,
                 news=None, info=None, bars=None, earnings=None):
        self._fast_info = fast_info
        self._closes = closes or []
        self._error = error
        # Lets a test make `.history()` raise something different from
        # `.fast_info` (e.g. a typed yfinance exception for check_ticker
        # coverage) without disturbing fast_info-based tests that only set
        # `error=`.
        self._history_error = history_error
        self._news = news or []
        self._info = info or {}
        self._bars = bars
        self._earnings = earnings

    @property
    def fast_info(self):
        if self._error:
            raise self._error
        return self._fast_info

    def history(self, period=None, interval=None, **kwargs):
        err = self._history_error or self._error
        if err:
            raise err
        if self._bars is not None:
            return self._bars
        return _Frame(self._closes)

    def get_earnings_dates(self, limit=8):
        if self._error:
            raise self._error
        return self._earnings

    @property
    def news(self):
        if self._error:
            raise self._error
        return self._news

    @property
    def info(self):
        if self._error:
            raise self._error
        return self._info


class RaisingFastInfoOnRead:
    """Mimics real yfinance: constructing/accessing `.fast_info` never raises
    on its own — it just builds a lazy wrapper. The actual HTTP call, and
    therefore the exception, happens when a property on it is *read*
    (`.last_price`, `.previous_close`, ...). Regression coverage for a bug
    where `check_ticker` wrapped `.fast_info` access in a try block but read
    `.last_price` outside it, letting a delisted-ticker or rate-limit error
    escape as an unhandled 500 instead of being classified."""

    def __init__(self, error):
        self._error = error

    @property
    def last_price(self):
        raise self._error

    @property
    def previous_close(self):
        raise self._error


def factory_for(mapping):
    def factory(ticker: str):
        if ticker not in mapping:
            raise ValueError(f"unknown ticker {ticker}")
        return mapping[ticker]

    return factory


def test_fetch_quotes_computes_change_pct():
    client = YFinanceClient(
        ticker_factory=factory_for(
            {"AAPL": FakeTicker(FakeFastInfo(110.0, 100.0, 1234, "USD"))}
        )
    )
    quotes = client.fetch_quotes(["AAPL"])
    assert quotes["AAPL"] == Quote(
        ticker="AAPL",
        price=110.0,
        prev_close=100.0,
        change_pct=10.0,
        volume=1234,
        currency="USD",
    )


def test_fetch_quotes_omits_failing_tickers_without_raising():
    client = YFinanceClient(
        ticker_factory=factory_for(
            {
                "AAPL": FakeTicker(FakeFastInfo(110.0, 100.0, 1, "USD")),
                "BAD": FakeTicker(error=RuntimeError("upstream down")),
            }
        )
    )
    quotes = client.fetch_quotes(["AAPL", "BAD"])
    assert set(quotes) == {"AAPL"}


def test_fetch_quotes_returns_empty_on_total_outage():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("down"))})
    )
    assert client.fetch_quotes(["AAPL"]) == {}


def test_fetch_quotes_handles_zero_prev_close():
    """Guards against ZeroDivisionError on newly listed or halted symbols."""
    client = YFinanceClient(
        ticker_factory=factory_for({"NEW": FakeTicker(FakeFastInfo(50.0, 0.0, 1, "USD"))})
    )
    assert client.fetch_quotes(["NEW"])["NEW"].change_pct == 0.0


def test_fetch_quotes_omits_ticker_with_none_price():
    client = YFinanceClient(
        ticker_factory=factory_for({"X": FakeTicker(FakeFastInfo(None, 100.0, 1, "USD"))})
    )
    assert client.fetch_quotes(["X"]) == {}


def test_fetch_intraday_returns_closes():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(closes=[1.0, 2.0, 3.0])})
    )
    assert client.fetch_intraday("AAPL") == [1.0, 2.0, 3.0]


def test_fetch_intraday_returns_empty_on_error():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("down"))})
    )
    assert client.fetch_intraday("AAPL") == []


def test_fetch_quotes_handles_exception_raised_on_fast_info_property_read():
    """Mimics real yfinance: constructing/accessing `.fast_info` never raises
    on its own — it just builds a lazy wrapper. The actual HTTP call, and
    therefore the exception, happens when a property on it is *read*
    (`.last_price`, `.previous_close`, ...). Regression coverage for a bug
    where a caller wrapped `.fast_info` access in a try block but read
    `.last_price` outside it, letting the error escape unhandled instead of
    being treated as a failed fetch."""
    client = YFinanceClient(
        ticker_factory=factory_for(
            {
                "AAPL": FakeTicker(
                    fast_info=RaisingFastInfoOnRead(KeyError("exchangeTimezoneName"))
                )
            }
        )
    )
    assert client.fetch_quotes(["AAPL"]) == {}


def test_validate_ticker_true_for_known_symbol():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(closes=[1.0, 2.0])})
    )
    assert client.validate_ticker("AAPL") is True


def test_validate_ticker_false_for_unknown_symbol():
    client = YFinanceClient(ticker_factory=factory_for({}))
    assert client.validate_ticker("ZZZZZZ") is False


def test_check_ticker_ok_for_known_symbol():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(closes=[1.0, 2.0, 3.0])})
    )
    assert client.check_ticker("AAPL") == "ok"


def test_check_ticker_unknown_when_result_is_empty():
    """No exception, but `history()` returns no rows — genuinely bad ticker."""
    client = YFinanceClient(ticker_factory=factory_for({"X": FakeTicker(closes=[])}))
    assert client.check_ticker("X") == "unknown"


def test_check_ticker_unknown_for_typo_or_delisted_symbol():
    """yfinance's typed 'this ticker doesn't exist' error must map to
    "unknown" (→ HTTP 400), not the conservative "unavailable" (→ 503) —
    this is the whole point of using history(raise_errors=True) over
    fast_info, whose failures were an untyped KeyError indistinguishable
    from a rate limit."""
    client = YFinanceClient(
        ticker_factory=factory_for(
            {
                "ZZZZZINVALIDTICKERXYZ": FakeTicker(
                    history_error=_TickerMissingError(
                        "ZZZZZINVALIDTICKERXYZ", "no price data found"
                    )
                )
            }
        )
    )
    assert client.check_ticker("ZZZZZINVALIDTICKERXYZ") == "unknown"


def test_check_ticker_unavailable_for_rate_limit():
    """A rate limit must not read as 'unknown' — the symbol may be fine."""
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(history_error=_RateLimitError())})
    )
    assert client.check_ticker("AAPL") == "unavailable"


def test_check_ticker_unavailable_for_generic_exception():
    """Anything unanticipated falls back to the conservative default."""
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("boom"))})
    )
    assert client.check_ticker("AAPL") == "unavailable"


def test_validate_ticker_false_when_upstream_unavailable():
    """validate_ticker keeps its boolean contract even for the unavailable case."""
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(history_error=_RateLimitError())})
    )
    assert client.validate_ticker("AAPL") is False


# --- News and Fundamentals Tests ---


def _article(article_id="a1", title="T", summary="S", pub="2026-08-10T20:29:18Z"):
    return {
        "id": article_id,
        "content": {
            "title": title,
            "summary": summary,
            "pubDate": pub,
            "provider": {"displayName": "Yahoo Finance"},
            "canonicalUrl": {"url": "https://example.com/a"},
        },
    }


def test_fetch_news_maps_nested_content():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(news=[_article()])})
    )
    articles = client.fetch_news("AAPL")
    assert len(articles) == 1
    a = articles[0]
    assert a.id == "a1"
    assert a.ticker == "AAPL"
    assert a.title == "T"
    assert a.summary == "S"
    assert a.publisher == "Yahoo Finance"
    assert a.url == "https://example.com/a"
    assert a.published_at == "2026-08-10T20:29:18Z"


def test_fetch_news_skips_articles_without_id():
    bad = {"content": {"title": "no id"}}
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(news=[bad, _article()])})
    )
    assert [a.id for a in client.fetch_news("AAPL")] == ["a1"]


def test_fetch_news_skips_articles_without_title():
    bad = {"id": "x", "content": {"summary": "s"}}
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(news=[bad, _article()])})
    )
    assert [a.id for a in client.fetch_news("AAPL")] == ["a1"]


def test_fetch_news_tolerates_missing_optional_fields():
    minimal = {"id": "m", "content": {"title": "Only a title"}}
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(news=[minimal])})
    )
    a = client.fetch_news("AAPL")[0]
    assert a.title == "Only a title"
    assert a.summary == ""
    assert a.publisher == ""
    assert a.url == ""


def test_fetch_news_returns_empty_on_error():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("down"))})
    )
    assert client.fetch_news("AAPL") == []


def test_fetch_news_returns_empty_when_no_news():
    client = YFinanceClient(ticker_factory=factory_for({"AAPL": FakeTicker(news=[])}))
    assert client.fetch_news("AAPL") == []


def test_fetch_news_skips_malformed_item_without_aborting():
    """A non-dict entry or malformed content must not crash the entire fetch.
    The per-item try/except is critical to protect against items where
    .get() would raise or iteration fails."""
    client = YFinanceClient(
        ticker_factory=factory_for({
            "AAPL": FakeTicker(news=[
                "not-a-dict",  # Genuinely malformed: not a dict at all
                _article(),     # Good article
                {"id": "x", "content": "oops"},  # Malformed: content is not a dict
            ])
        })
    )
    articles = client.fetch_news("AAPL")
    assert [a.id for a in articles] == ["a1"]


def test_fetch_fundamentals_maps_info_fields():
    info = {
        "trailingPE": 35.9, "forwardPE": 32.4, "marketCap": 4498802081792,
        "trailingEps": 8.57, "totalRevenue": 466822987776, "sector": "Technology",
        "industry": "Consumer Electronics", "dividendYield": 0.35, "beta": 1.086,
        "fiftyTwoWeekHigh": 344.57, "fiftyTwoWeekLow": 223.78,
    }
    client = YFinanceClient(ticker_factory=factory_for({"AAPL": FakeTicker(info=info)}))
    f = client.fetch_fundamentals("AAPL")
    assert f.ticker == "AAPL"
    assert f.pe_ratio == 35.9
    assert f.market_cap == 4498802081792
    assert f.sector == "Technology"
    assert f.week52_low == 223.78


def test_fetch_fundamentals_tolerates_missing_fields():
    """ETFs and ADRs routinely lack most of these."""
    client = YFinanceClient(
        ticker_factory=factory_for({"SPY": FakeTicker(info={"sector": None})})
    )
    f = client.fetch_fundamentals("SPY")
    assert f is not None
    assert f.pe_ratio is None
    assert f.sector is None


def test_fetch_fundamentals_returns_none_on_error():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("down"))})
    )
    assert client.fetch_fundamentals("AAPL") is None


def test_fetch_fundamentals_returns_none_on_empty_info():
    client = YFinanceClient(ticker_factory=factory_for({"AAPL": FakeTicker(info={})}))
    assert client.fetch_fundamentals("AAPL") is None


# --- Bars and Earnings Tests ---


def _bar_frame():
    idx = pd.date_range("2026-01-01 09:00", periods=3, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"Open": [1.0, 2.0, 3.0], "High": [2.0, 3.0, 4.0], "Low": [0.5, 1.5, 2.5],
         "Close": [1.5, 2.5, 3.5], "Volume": [100.0, 200.0, 300.0]},
        index=idx,
    )


def test_fetch_bars_maps_ohlcv_and_utc_timestamps():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(bars=_bar_frame())})
    )
    bars = client.fetch_bars("AAPL", "60d", "1h")
    assert len(bars) == 3
    assert bars[0]["open"] == 1.0 and bars[0]["close"] == 1.5
    assert bars[0]["ts"].endswith("+00:00")
    assert [b["close"] for b in bars] == [1.5, 2.5, 3.5], "oldest-first"


def test_fetch_bars_returns_empty_on_error():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=RuntimeError("down"))})
    )
    assert client.fetch_bars("AAPL", "60d", "1h") == []


def test_fetch_bars_returns_empty_on_empty_frame():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(bars=pd.DataFrame())})
    )
    assert client.fetch_bars("AAPL", "60d", "1h") == []


def _earnings_frame():
    idx = pd.DatetimeIndex(["2026-10-29 20:00", "2026-07-30 20:00"], tz="UTC")
    return pd.DataFrame({"EPS Estimate": [1.98, float("nan")]}, index=idx)


def test_fetch_earnings_dates_maps_rows():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(earnings=_earnings_frame())})
    )
    rows = client.fetch_earnings_dates("AAPL")
    assert len(rows) == 2
    assert rows[0]["earnings_at"].startswith("2026-10-29")
    assert rows[0]["eps_estimate"] == 1.98


def test_fetch_earnings_nan_estimate_becomes_none():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(earnings=_earnings_frame())})
    )
    assert client.fetch_earnings_dates("AAPL")[1]["eps_estimate"] is None


def test_fetch_earnings_returns_empty_on_error():
    client = YFinanceClient(
        ticker_factory=factory_for({"AAPL": FakeTicker(error=ImportError("lxml"))})
    )
    assert client.fetch_earnings_dates("AAPL") == []
