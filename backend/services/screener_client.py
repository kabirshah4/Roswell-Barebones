"""Pulls the searchable symbol catalogue from a market screener endpoint.

Roswell ships without a screener source. Set ``SCREENER_SCAN_URL`` to a
screener that accepts a JSON POST of ``{filter, columns, range, sort}`` and
answers with ``{"data": [{"s": "NASDAQ:AAPL", "d": [...]}, ...]}`` — the shape
this module parses. With the variable unset the catalogue simply stays empty
and every caller degrades to a clear "no screener source configured" state.

The catalogue is for FINDING a ticker, not for trusting one. A screener and
Yahoo will disagree about symbol formatting and coverage, so adding a name
still goes through the existing Yahoo validation — this only means you no
longer have to already know the symbol you want.

The fifth and last module permitted to import httpx.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


def scanner_url(market: str) -> str | None:
    """The scan endpoint for one market, or None when unconfigured.

    ``{market}`` in the template is substituted; a template without the
    placeholder is used as-is, which is what a single-market screener wants.
    """
    template = os.environ.get("SCREENER_SCAN_URL", "").strip()
    if not template:
        return None
    return template.replace("{market}", market) if "{market}" in template else template


def screener_configured() -> bool:
    return bool(os.environ.get("SCREENER_SCAN_URL", "").strip())


_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

_COLUMNS = ["name", "description", "exchange", "type", "market_cap_basic"]

# Each asset class lives in its own scanner market with its own columns, and
# each needs a different rule to reach a symbol yfinance will actually quote.
# Only classes that survive that mapping are offered -- a category the app
# cannot price is worse than no category.
#
# Deliberately excluded: futures (53,264 rows of `EUREX:EAIFJ2027`) and the
# wider crypto set (61,924 rows including `PANCAKESWAP:BTCUSDT_5840B7.USD`).
# Neither maps to anything quotable.
_MARKETS = {
    "stock": {"market": "america", "type": "stock"},
    "fund": {"market": "america", "type": "fund"},
    "dr": {"market": "america", "type": "dr"},
    "crypto": {
        "market": "crypto",
        "exchange": "COINBASE",
        "columns": ["name", "base_currency", "currency", "description",
                    "market_cap_calc"],
    },
    "forex": {
        "market": "forex",
        "exchange": "FX_IDC",
        "columns": ["name", "description"],
    },
}

# yfinance quotes these; a stock screener typically returns nothing for
# type=index, so they are curated rather than fetched.
INDICES = (
    ("^GSPC", "S&P 500"), ("^DJI", "Dow Jones Industrial Average"),
    ("^IXIC", "Nasdaq Composite"), ("^RUT", "Russell 2000"),
    ("^VIX", "CBOE Volatility Index"), ("^FTSE", "FTSE 100"),
    ("^N225", "Nikkei 225"), ("^GDAXI", "DAX"),
    ("^HSI", "Hang Seng"), ("^TNX", "10-Year Treasury Yield"),
)

# Generous: the whole catalogue is ~20k rows and arrives in one response.
_PAGE = 25_000


# Forex and indices have no market cap, so alphabetical order is all that is
# left -- and a FOREX tab whose first row is AEDAUD is useless. These get an
# explicit rank instead; everything else still sorts by size.
MAJOR_PAIRS = ("EURUSD", "USDJPY", "GBPUSD", "USDCHF", "AUDUSD", "USDCAD",
               "NZDUSD", "EURGBP", "EURJPY", "GBPJPY", "USDCNY", "USDMXN")


@dataclass(frozen=True)
class Symbol:
    symbol: str        # yfinance form, e.g. BRK-B
    source_symbol: str     # screener form, e.g. NASDAQ:BRK.B
    name: str
    exchange: str
    kind: str          # stock | fund | dr | crypto | forex | index
    market_cap: float | None
    sort_rank: int = 0  # higher first; only used where market cap is absent


def to_yahoo_symbol(source_symbol: str) -> str:
    """Convert an exchange-qualified symbol to the form yfinance expects.

    Two differences that matter: screeners prefix the exchange
    (``NASDAQ:AAPL``), and it writes share classes with a dot (``BRK.B``) where
    Yahoo wants a dash (``BRK-B``). The dot form silently returns a quote with
    no market cap rather than failing, which is how it slips through unnoticed.
    """
    bare = source_symbol.split(":", 1)[-1].strip().upper()
    return bare.replace(".", "-")


def _default_poster(url: str, payload: dict) -> Any:
    import httpx

    with httpx.Client(timeout=60.0, headers=_HEADERS) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def _parse_row(row: dict, kind: str) -> Symbol | None:
    """One scanner row to a Symbol, or None if it cannot be mapped.

    One malformed row must not discard twenty thousand good ones.
    """
    try:
        source_symbol = row["s"]
        data = row["d"]

        if kind == "crypto":
            name, base, currency, description, cap = (data + [None] * 5)[:5]
            if not base or not currency:
                return None
            return Symbol(
                symbol=f"{base}-{currency}".upper(),
                source_symbol=source_symbol,
                name=str(description or name or ""),
                exchange="CRYPTO",
                kind="crypto",
                market_cap=float(cap) if cap is not None else None,
            )

        if kind == "forex":
            name, description = (data + [None] * 2)[:2]
            pair = str(name or source_symbol.split(":")[-1]).upper()
            if len(pair) != 6:
                # Anything that is not a plain six-letter pair does not map to
                # yfinance's XXXYYY=X form.
                return None
            rank = (
                len(MAJOR_PAIRS) - MAJOR_PAIRS.index(pair)
                if pair in MAJOR_PAIRS else 0
            )
            return Symbol(
                symbol=f"{pair}=X", source_symbol=source_symbol,
                name=str(description or pair), exchange="FOREX",
                kind="forex", market_cap=None, sort_rank=rank,
            )

        name, description, exchange, row_kind, cap = (data + [None] * 5)[:5]
        return Symbol(
            symbol=to_yahoo_symbol(source_symbol),
            source_symbol=source_symbol,
            name=str(description or name or ""),
            exchange=str(exchange or ""),
            kind=str(row_kind or kind),
            market_cap=float(cap) if cap is not None else None,
        )
    except Exception:
        return None


class ScreenerClient:
    def __init__(self, poster: Callable[[str, dict], Any] | None = None) -> None:
        self._poster = poster or _default_poster

    @property
    def enabled(self) -> bool:
        # No credential of any kind -- but without an endpoint there is
        # nothing to call, and callers need to say so rather than retry.
        return screener_configured()

    def fetch_symbols(
        self, kinds: tuple[str, ...] = ("stock", "fund", "dr", "crypto", "forex")
    ) -> list[Symbol]:
        """The whole catalogue. Returns [] on failure; never raises."""
        out: list[Symbol] = []
        seen: set[str] = set()

        for position, (ticker, name) in enumerate(INDICES):
            seen.add(ticker)
            out.append(Symbol(ticker, ticker, name, "INDEX", "index", None,
                              sort_rank=len(INDICES) - position))

        for kind in kinds:
            for symbol in self._fetch_kind(kind):
                # A share class can appear under several screener symbols
                # that collapse to one Yahoo symbol; keep the first.
                if symbol.symbol in seen:
                    continue
                seen.add(symbol.symbol)
                out.append(symbol)
        return out

    def _fetch_kind(self, kind: str) -> list[Symbol]:
        spec = _MARKETS.get(kind)
        if spec is None:
            return []

        columns = spec.get("columns", _COLUMNS)
        if "type" in spec:
            filters = [{"left": "type", "operation": "equal", "right": spec["type"]}]
            sort = {"sortBy": "market_cap_basic", "sortOrder": "desc"}
        else:
            filters = [
                {"left": "exchange", "operation": "equal", "right": spec["exchange"]}
            ]
            sort = (
                {"sortBy": "market_cap_calc", "sortOrder": "desc"}
                if kind == "crypto" else None
            )

        payload: dict = {"filter": filters, "columns": columns, "range": [0, _PAGE]}
        if sort:
            payload["sort"] = sort

        url = scanner_url(spec["market"])
        if url is None:
            return []

        try:
            body = self._poster(url, payload)
        except Exception as exc:
            logger.warning(
                "Symbol fetch failed for %s (%s)", kind, type(exc).__name__
            )
            return []

        rows = (body or {}).get("data") or []
        out: list[Symbol] = []
        for row in rows:
            symbol = _parse_row(row, kind)
            if symbol is not None:
                out.append(symbol)
        logger.info("Screener returned %d %s symbols", len(out), kind)
        return out
