"""The only module in the project that knows Yahoo Finance exists.

When yfinance breaks upstream — it is unofficial, so it will — this is the
single file to repair.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from yfinance.exceptions import YFRateLimitError, YFTickerMissingError

logger = logging.getLogger(__name__)

# Roughly one trading session of 5-minute bars. Enough for the sparkline to
# have a shape, few enough that it is still about today.
SPARKLINE_POINTS = 78

# Exposed so tests can raise/catch the exact types `check_ticker` handles
# without importing yfinance themselves (only this module is allowed to).
TICKER_MISSING_ERRORS: tuple[type[Exception], ...] = (YFTickerMissingError,)
RATE_LIMIT_ERRORS: tuple[type[Exception], ...] = (YFRateLimitError,)


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    prev_close: float
    change_pct: float
    volume: int
    currency: str


@dataclass(frozen=True)
class NewsArticle:
    id: str
    ticker: str
    title: str
    publisher: str
    url: str
    published_at: str
    summary: str


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    pe_ratio: float | None
    forward_pe: float | None
    market_cap: float | None
    eps: float | None
    revenue: float | None
    sector: str | None
    industry: str | None
    dividend_yield: float | None
    beta: float | None
    week52_high: float | None
    week52_low: float | None
    # Extended set. Every one is optional -- ETFs, ADRs and newly listed names
    # routinely have none of them, which is not an error.
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    analyst_count: float | None = None
    peg_ratio: float | None = None
    price_to_book: float | None = None
    profit_margin: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    return_on_equity: float | None = None
    debt_to_equity: float | None = None
    free_cashflow: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    short_pct_float: float | None = None
    held_by_institutions: float | None = None
    avg_volume: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    payout_ratio: float | None = None
    recommendation: str | None = None


def _default_ticker_factory(ticker: str) -> Any:
    import yfinance as yf

    return yf.Ticker(ticker)


class YFinanceClient:
    def __init__(self, ticker_factory: Callable[[str], Any] | None = None) -> None:
        self._ticker_factory = ticker_factory or _default_ticker_factory

    def fetch_quotes(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Fetch quotes, omitting any ticker that fails.

        Individual failures never raise; the caller distinguishes a partial
        result from a total outage by comparing keys against what it requested.
        """
        quotes: dict[str, Quote] = {}
        for ticker in tickers:
            try:
                info = self._ticker_factory(ticker).fast_info
                price = info.last_price
                prev_close = info.previous_close
                if price is None or prev_close is None:
                    logger.warning("Incomplete quote for %s; skipping", ticker)
                    continue
                change_pct = (
                    ((price - prev_close) / prev_close) * 100.0 if prev_close else 0.0
                )
                quotes[ticker] = Quote(
                    ticker=ticker,
                    price=float(price),
                    prev_close=float(prev_close),
                    change_pct=round(change_pct, 4),
                    volume=int(info.last_volume or 0),
                    currency=info.currency or "USD",
                )
            except Exception:
                logger.warning("Quote fetch failed for %s", ticker, exc_info=True)
        return quotes

    def fetch_intraday(self, ticker: str) -> list[float]:
        """5-minute closes for the watchlist sparkline, or [] if unavailable.

        Two days, not one, and then trimmed to the tail. Asking for a single
        day means that at 09:41 the market has produced three bars, and a
        three-point sparkline is a straight line — which is what the trend
        column showed every morning. Reaching back a session guarantees a real
        shape at any hour, and trimming keeps it to roughly one session's worth
        so the line still says something about now.
        """
        try:
            frame = self._ticker_factory(ticker).history(period="2d", interval="5m")
            if getattr(frame, "empty", False):
                return []
            closes = [float(v) for v in frame["Close"].dropna().tolist()]
            return closes[-SPARKLINE_POINTS:]
        except Exception:
            logger.warning("Intraday fetch failed for %s", ticker, exc_info=True)
            return []

    def check_ticker(self, ticker: str) -> Literal["ok", "unknown", "unavailable"]:
        """Distinguish a genuinely bad symbol from a transient upstream failure.

        Callers that need to tell a user "that symbol doesn't exist" apart
        from "try again in a moment" must use this instead of
        ``validate_ticker``.

        - ``"ok"``: the symbol resolved and carries usable price data.
        - ``"unknown"``: the symbol resolved without raising but carries no
          usable price data, or yfinance raised its typed "this ticker is
          missing" error (``YFTickerMissingError`` and its subclasses, e.g.
          ``YFPricesMissingError`` for a typo'd symbol like
          "ZZZZZINVALIDTICKERXYZ", ``YFTzMissingError`` for a delisted one).
        - ``"unavailable"``: the upstream call raised for any other reason
          (rate limit, network error, timeout, an unanticipated internal
          yfinance error, ...); we cannot say anything conclusive about the
          symbol, so we do not accuse it of being invalid.

        Uses ``history()`` rather than ``fast_info``: unlike ``fast_info``
        (whose failures surface as an untyped ``KeyError`` deep inside
        yfinance, indistinguishable by type from a rate limit),
        ``history(..., raise_errors=True)`` surfaces yfinance's typed
        exception hierarchy, letting us classify the failure instead of
        conservatively treating every failure as "unavailable". Rate limits
        (``YFRateLimitError``) always surface as that distinct type from
        inside yfinance regardless of ``raise_errors``; ``raise_errors=True``
        is only needed to also surface ``YFTickerMissingError``. We use the
        deprecated per-call kwarg rather than the replacement
        ``yf.config.debug.hide_exceptions`` because the latter is
        process-wide mutable state shared across concurrent requests.
        """
        try:
            frame = self._ticker_factory(ticker).history(
                period="5d", interval="1d", raise_errors=True
            )
        except TICKER_MISSING_ERRORS:
            logger.info("Ticker check: unknown symbol %s", ticker)
            return "unknown"
        except RATE_LIMIT_ERRORS:
            logger.warning("Ticker check: rate limited checking %s", ticker)
            return "unavailable"
        except Exception:
            logger.warning("Ticker check failed for %s", ticker, exc_info=True)
            return "unavailable"
        if getattr(frame, "empty", True):
            return "unknown"
        return "ok"

    def validate_ticker(self, ticker: str) -> bool:
        """True if the symbol resolves to a real, priced instrument.

        Kept for existing callers; treats both "unknown" and "unavailable" as
        not-valid. Callers that must react differently to a transient
        upstream failure should use ``check_ticker`` instead.
        """
        return self.check_ticker(ticker) == "ok"

    def fetch_news(self, ticker: str) -> list[NewsArticle]:
        """Return recent articles for a ticker, or [] if unavailable.

        Yahoo nests the payload under a `content` key and occasionally omits
        fields; anything without a stable id or a title is skipped rather than
        stored, because both are load-bearing downstream.
        """
        try:
            raw = self._ticker_factory(ticker).news or []
        except Exception:
            logger.warning("News fetch failed for %s", ticker, exc_info=True)
            return []

        articles: list[NewsArticle] = []
        for item in raw:
            try:
                article_id = item.get("id")
                content = item.get("content") or {}
                title = content.get("title")
                if not article_id or not title:
                    continue
                provider = content.get("provider") or {}
                canonical = content.get("canonicalUrl") or {}
                articles.append(
                    NewsArticle(
                        id=str(article_id),
                        ticker=ticker,
                        title=str(title),
                        publisher=str(provider.get("displayName") or ""),
                        url=str(canonical.get("url") or ""),
                        published_at=str(content.get("pubDate") or ""),
                        summary=str(content.get("summary") or ""),
                    )
                )
            except Exception:
                logger.warning("Skipping malformed news item for %s", ticker, exc_info=True)
        return articles

    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None:
        """Return key ratios for a ticker, or None if unavailable.

        Individual fields are frequently absent (ETFs, ADRs, newly listed names);
        those become None rather than failing the whole fetch.
        """
        try:
            info = self._ticker_factory(ticker).info or {}
        except Exception:
            logger.warning("Fundamentals fetch failed for %s", ticker, exc_info=True)
            return None

        if not info:
            return None

        def num(key: str) -> float | None:
            value = info.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def text(key: str) -> str | None:
            value = info.get(key)
            return str(value) if value else None

        return Fundamentals(
            ticker=ticker,
            pe_ratio=num("trailingPE"),
            forward_pe=num("forwardPE"),
            market_cap=num("marketCap"),
            eps=num("trailingEps"),
            revenue=num("totalRevenue"),
            sector=text("sector"),
            industry=text("industry"),
            dividend_yield=num("dividendYield"),
            beta=num("beta"),
            week52_high=num("fiftyTwoWeekHigh"),
            week52_low=num("fiftyTwoWeekLow"),
            target_mean=num("targetMeanPrice"),
            target_high=num("targetHighPrice"),
            target_low=num("targetLowPrice"),
            analyst_count=num("numberOfAnalystOpinions"),
            peg_ratio=num("trailingPegRatio"),
            price_to_book=num("priceToBook"),
            profit_margin=num("profitMargins"),
            gross_margin=num("grossMargins"),
            operating_margin=num("operatingMargins"),
            return_on_equity=num("returnOnEquity"),
            debt_to_equity=num("debtToEquity"),
            free_cashflow=num("freeCashflow"),
            revenue_growth=num("revenueGrowth"),
            earnings_growth=num("earningsGrowth"),
            short_pct_float=num("shortPercentOfFloat"),
            held_by_institutions=num("heldPercentInstitutions"),
            avg_volume=num("averageVolume"),
            ma50=num("fiftyDayAverage"),
            ma200=num("twoHundredDayAverage"),
            payout_ratio=num("payoutRatio"),
            recommendation=text("recommendationKey"),
        )

    def fetch_expirations(self, ticker: str) -> list[str]:
        """Option expiry dates, nearest first. [] when the name has no options."""
        try:
            return list(self._ticker_factory(ticker).options or [])
        except Exception:
            logger.warning("Expirations unavailable for %s", ticker, exc_info=True)
            return []

    def fetch_option_chain(self, ticker: str, expiry: str) -> dict:
        """Calls and puts for one expiry, as plain rows.

        Every numeric field is coerced and NaN-checked here rather than in the
        route: yfinance returns NaN for an untraded contract's bid, ask and
        implied volatility, and NaN does not survive JSON.
        """
        try:
            chain = self._ticker_factory(ticker).option_chain(expiry)
        except Exception:
            logger.warning(
                "Option chain unavailable for %s %s", ticker, expiry, exc_info=True
            )
            return {"calls": [], "puts": []}

        def num(value):
            try:
                out = float(value)
                return None if out != out else out
            except (TypeError, ValueError):
                return None

        def rows(frame):
            if frame is None or getattr(frame, "empty", True):
                return []
            out = []
            for _, row in frame.iterrows():
                strike = num(row.get("strike"))
                if strike is None:
                    continue
                out.append({
                    "contract": str(row.get("contractSymbol") or ""),
                    "strike": strike,
                    "last": num(row.get("lastPrice")),
                    "bid": num(row.get("bid")),
                    "ask": num(row.get("ask")),
                    "change_pct": num(row.get("percentChange")),
                    "volume": num(row.get("volume")) or 0,
                    "open_interest": num(row.get("openInterest")) or 0,
                    "iv": num(row.get("impliedVolatility")),
                    "in_the_money": bool(row.get("inTheMoney")),
                })
            return out

        return {"calls": rows(chain.calls), "puts": rows(chain.puts)}

    def fetch_company(self, ticker: str) -> dict:
        """Financial statements, analyst ratings and estimates for one company.

        Backs the DES / FA / ANR / EE functions. Every section is optional --
        ETFs have no income statement, newly listed names have no estimates --
        so each is fetched independently and a failure yields an empty section
        rather than losing the rest.

        Returns plain JSON-ready structures; pandas frames do not survive a
        round trip through SQLite.
        """
        handle = self._ticker_factory(ticker)
        out: dict = {}

        def frame(name: str, attr: str, limit: int = 5) -> None:
            try:
                df = getattr(handle, attr)
                if df is None or getattr(df, "empty", True):
                    return
                periods = [str(c)[:10] for c in list(df.columns)[:limit]]
                rows = []
                for label, series in df.iterrows():
                    values = []
                    for column in list(df.columns)[:limit]:
                        value = series.get(column)
                        try:
                            value = None if value is None else float(value)
                            if value != value:      # NaN
                                value = None
                        except (TypeError, ValueError):
                            value = None
                        values.append(value)
                    if any(v is not None for v in values):
                        rows.append({"label": str(label), "values": values})
                out[name] = {"periods": periods, "rows": rows}
            except Exception:
                logger.warning("%s unavailable for %s", attr, ticker, exc_info=True)

        frame("income_statement", "income_stmt")
        frame("balance_sheet", "balance_sheet")
        frame("cashflow", "cashflow")
        frame("earnings_estimate", "earnings_estimate", limit=8)
        frame("revenue_estimate", "revenue_estimate", limit=8)


        try:
            # Not a time series: yfinance returns one ROW per period with the
            # rating buckets as COLUMNS, so the generic frame reader above would
            # transpose it into nonsense.
            recs = handle.recommendations
            if recs is not None and not getattr(recs, "empty", True):
                out["recommendations"] = [
                    {
                        "period": str(row.get("period", "")),
                        "strong_buy": int(row.get("strongBuy") or 0),
                        "buy": int(row.get("buy") or 0),
                        "hold": int(row.get("hold") or 0),
                        "sell": int(row.get("sell") or 0),
                        "strong_sell": int(row.get("strongSell") or 0),
                    }
                    for _, row in recs.iterrows()
                ]
        except Exception:
            logger.warning("Recommendations unavailable for %s", ticker, exc_info=True)

        try:
            targets = handle.analyst_price_targets or {}
            out["price_targets"] = {
                k: (float(v) if v is not None else None)
                for k, v in targets.items()
                if isinstance(v, (int, float))
            }
        except Exception:
            logger.warning("Price targets unavailable for %s", ticker, exc_info=True)

        try:
            holders = handle.institutional_holders
            if holders is not None and not getattr(holders, "empty", True):
                out["institutional_holders"] = [
                    {
                        "holder": str(row.get("Holder", "")),
                        "shares": float(row.get("Shares") or 0),
                        "pct_held": float(row.get("pctHeld") or 0),
                        "value": float(row.get("Value") or 0),
                    }
                    for _, row in holders.head(10).iterrows()
                ]
        except Exception:
            logger.warning("Holders unavailable for %s", ticker, exc_info=True)

        try:
            info = handle.info or {}
            out["profile"] = {
                key: info.get(source)
                for key, source in (
                    ("name", "longName"), ("summary", "longBusinessSummary"),
                    ("website", "website"), ("country", "country"),
                    ("employees", "fullTimeEmployees"), ("city", "city"),
                    ("exchange", "fullExchangeName"), ("currency", "currency"),
                )
            }
        except Exception:
            logger.warning("Profile unavailable for %s", ticker, exc_info=True)

        return out

    def fetch_bars(self, ticker: str, period: str, interval: str) -> list[dict]:
        """OHLCV bars as plain dicts, oldest-first. [] on failure; never raises.

        Timestamps are normalised to UTC ISO-8601 strings so nothing downstream
        has to reason about yfinance's timezone-aware index.
        """
        try:
            frame = self._ticker_factory(ticker).history(
                period=period, interval=interval
            )
            if getattr(frame, "empty", True):
                return []
            out: list[dict] = []
            for ts, row in frame.iterrows():
                out.append(
                    {
                        "ts": ts.tz_convert("UTC").isoformat()
                        if getattr(ts, "tzinfo", None)
                        else str(ts),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                )
            return out
        except Exception:
            logger.warning(
                "Bar fetch failed for %s %s", ticker, interval, exc_info=True
            )
            return []

    def fetch_earnings_dates(self, ticker: str, limit: int = 8) -> list[dict]:
        """Upcoming and recent earnings dates. [] on failure; never raises.

        Requires lxml. Without it yfinance raises ImportError here, and a silent
        [] would make the blackout look like it passed rather than never ran —
        so the failure is logged explicitly.
        """
        try:
            frame = self._ticker_factory(ticker).get_earnings_dates(limit=limit)
            if frame is None or getattr(frame, "empty", True):
                return []
            out: list[dict] = []
            for ts, row in frame.iterrows():
                estimate = row.get("EPS Estimate")
                try:
                    estimate = float(estimate)
                except (TypeError, ValueError):
                    estimate = None
                if estimate is not None and estimate != estimate:  # NaN
                    estimate = None
                out.append(
                    {
                        "earnings_at": ts.tz_convert("UTC").isoformat()
                        if getattr(ts, "tzinfo", None)
                        else str(ts),
                        "eps_estimate": estimate,
                    }
                )
            return out
        except Exception:
            logger.warning("Earnings fetch failed for %s", ticker, exc_info=True)
            return []
