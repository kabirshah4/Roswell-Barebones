"""Tools the chat model may call.

Every executor reads SQLite and nothing else. A chat turn must never trigger a
Yahoo, FRED, or Discord request - the poller owns all outbound market-data
traffic, and a conversational interface that fanned out network calls per
message would be slow, expensive, and would break this project's rate-limit
discipline.

None of these tools invents a number. Price levels come from the `signals`
table, where the signal engine put them after deriving them from ATR and swing
structure.
"""

from pathlib import Path
from typing import Any

from backend.db import database

_TICKER = {"ticker": {"type": "string", "description": "Ticker symbol, e.g. AAPL"}}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_watchlist",
        "description": "List the tickers the user is currently tracking.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_prices",
        "description": (
            "Latest cached price and percent change for every watchlist ticker. "
            "is_stale means the last refresh failed and the value may be old."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_news",
        "description": (
            "Recent headlines for a ticker, each with an AI one-line summary and a "
            "bullish/bearish/neutral tag when enrichment has run."
        ),
        "input_schema": {"type": "object", "properties": _TICKER, "required": ["ticker"]},
    },
    {
        "name": "get_fundamentals",
        "description": "P/E, market cap, EPS, revenue, sector, beta and 52-week range.",
        "input_schema": {"type": "object", "properties": _TICKER, "required": ["ticker"]},
    },
    {
        "name": "get_signals",
        "description": (
            "Computed trade setups with entry, stop and two targets. These levels come "
            "from ATR and swing structure - never invent your own. Always pair a grade "
            "with get_backtest_stats, because measurement showed the A+ grade does not "
            "reliably outperform B."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_backtest_stats",
        "description": (
            "Measured historical win rate and average R per grade for a ticker, from "
            "replaying the signal engine over cached daily bars. Use this whenever you "
            "mention a grade so the letter does not overclaim."
        ),
        "input_schema": {"type": "object", "properties": _TICKER, "required": ["ticker"]},
    },
    {
        "name": "get_earnings",
        "description": (
            "Next confirmed earnings date for a ticker, or null if unknown. A setup near "
            "earnings carries gap risk the stop does not account for."
        ),
        "input_schema": {"type": "object", "properties": _TICKER, "required": ["ticker"]},
    },
    {
        "name": "run_screener",
        "description": (
            "Filter the cached S&P 500 universe. Always report the coverage figure - the "
            "universe warms slowly, and partial coverage must not be read as a full scan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pe_min": {"type": "number"}, "pe_max": {"type": "number"},
                "market_cap_min": {"type": "number"},
                "dividend_yield_min": {"type": "number"},
                "sector": {"type": "string"},
            },
        },
    },
    {
        "name": "get_price_outlook",
        "description": (
            "Forward-looking price projection for a ticker, computed from cached "
            "daily bars: measured annualised volatility, a damped trend estimate, "
            "and expected ranges (68% and 95%) over roughly one week, one month "
            "and one quarter. Optionally pass target_price to get the probability "
            "of finishing above it. USE THIS for any 'where is it going' / 'will "
            "it hit X' / 'what's the upside' question instead of guessing. Always "
            "quote the range, not just the central estimate - the range is what "
            "the evidence supports. The model assumes the future resembles the "
            "recent past, so pair it with get_earnings and get_news, which is "
            "where that assumption breaks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_TICKER,
                "target_price": {
                    "type": "number",
                    "description": "Optional: price to compute the probability of exceeding.",
                },
                "horizon_days": {
                    "type": "number",
                    "description": "Trading days for the target probability. Default 21.",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_link_health",
        "description": (
            "Health of the cached news links: a count per status, plus every article "
            "whose URL failed validation. status 'dead' means the publisher returned "
            "404/410 - the article is gone. 'error' means the host did not respond at "
            "all. 'unchecked' means it has not been validated yet, which is not the "
            "same as working. Report the unchecked count whenever you summarise, so a "
            "partial sweep is not read as a clean bill of health."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_macro_calendar",
        "description": "Upcoming curated economic releases with impact tiers.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _sym(args: dict) -> str:
    return str(args.get("ticker", "")).strip().upper()


def execute(name: str, args: dict, db_path: Path) -> dict[str, Any]:
    """Run one tool. Never raises; failures come back as {"error": str}."""
    rows: list[dict] = []
    try:
        with database.get_conn(db_path) as conn:
            if name == "get_watchlist":
                return {"tickers": database.list_watchlist(conn)}

            if name == "get_prices":
                tickers = database.list_watchlist(conn)
                return {"prices": database.get_prices(conn, tickers)}

            if name == "get_news":
                return {"articles": database.list_news(conn, _sym(args), limit=10)}

            if name == "get_fundamentals":
                return {"fundamentals": database.get_fundamentals(conn, _sym(args))}

            if name == "get_signals":
                return {"signals": database.list_signals(conn, limit=20)}

            if name == "get_earnings":
                from datetime import datetime, timezone

                return {
                    "next_earnings": database.next_earnings(
                        conn, _sym(args), datetime.now(timezone.utc).isoformat()
                    )
                }

            if name == "get_price_outlook":
                return _price_outlook(conn, args)

            if name == "get_link_health":
                return {
                    "summary": database.link_summary(conn),
                    "broken": database.broken_links(conn, limit=25),
                }

            if name == "get_macro_calendar":
                return {"events": database.list_macro_events(conn, limit=15)}

            if name == "run_screener":
                from backend.services import screener as engine

                filters = engine.Filters(
                    pe_min=args.get("pe_min"), pe_max=args.get("pe_max"),
                    market_cap_min=args.get("market_cap_min"),
                    dividend_yield_min=args.get("dividend_yield_min"),
                    sector=args.get("sector"), limit=25,
                )
                engine.validate(filters)
                return {
                    "matches": engine.run_screen(conn, filters),
                    "coverage": database.universe_coverage(conn),
                }

            if name == "get_backtest_stats":
                rows = database.get_bars(conn, _sym(args), "1d", limit=2000)
            else:
                return {"error": f"unknown tool: {name}"}

        # Backtest runs outside the connection: it is pure CPU over cached rows.
        if not rows:
            return {"note": "no cached daily bars for this ticker yet"}

        import pandas as pd

        from backend.services.backtest import run_backtest

        frame = pd.DataFrame(rows)
        frame.index = pd.to_datetime(frame["ts"], utc=True)
        frame = frame.rename(
            columns={"open": "Open", "high": "High", "low": "Low",
                     "close": "Close", "volume": "Volume"}
        )
        stats = run_backtest(_sym(args), frame)
        if not stats:
            return {"note": "not enough cached daily history to backtest yet"}
        return {"stats": {g: vars(s) for g, s in stats.items()}}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _price_outlook(conn: Any, args: dict) -> dict[str, Any]:
    """Build a projection from cached daily bars.

    The engine is pure, so this is the adapter: bars out of SQLite, frame in,
    plain dict out. No network call, in keeping with the tools' read-only rule.
    """
    import pandas as pd

    from backend.services import forecast

    ticker = _sym(args)
    rows = database.get_bars(conn, ticker, "1d", limit=400)
    if len(rows) < forecast.MIN_BARS:
        return {
            "error": (
                f"No cached daily bars for {ticker} yet (have {len(rows)}, need "
                f"{forecast.MIN_BARS}). The poller fills these in on its bar "
                f"cycle. Do not estimate a projection without them."
            )
        }

    frame = pd.DataFrame(rows)
    frame["Close"] = frame["close"].astype(float)
    outlook = forecast.build(ticker, frame)
    if outlook is None:
        return {"error": f"Could not build an outlook for {ticker}."}

    out: dict[str, Any] = {
        "ticker": outlook.ticker,
        "last_close": outlook.last_close,
        "annualised_volatility_pct": round(outlook.annualised_vol * 100, 2),
        "trend_note": outlook.momentum_note,
        "position_in_52w_range": outlook.range_position,
        "bands": [
            {
                "horizon_trading_days": b.horizon_days,
                "central_estimate": b.expected,
                "range_68pct": [b.low_68, b.high_68],
                "range_95pct": [b.low_95, b.high_95],
            }
            for b in outlook.bands
        ],
        "basis": (
            "Lognormal random walk using realised volatility over the last 60 "
            "sessions and a damped drift term. Assumes the future resembles the "
            "recent past; earnings and news are what break that."
        ),
    }

    target = args.get("target_price")
    if target is not None:
        days = int(args.get("horizon_days") or 21)
        probability = forecast.probability_above(outlook, float(target), days)
        out["target"] = {
            "price": float(target),
            "horizon_trading_days": days,
            "probability_above": probability,
        }
    return out
