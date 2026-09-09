"""The function catalogue — this terminal's answer to Bloomberg's command line.

Bloomberg navigates by typing a short code rather than hunting through menus.
That is the part worth copying: it is fast, it is memorable, and it scales to
far more screens than a menu bar can hold.

Only functions backed by data this app can actually obtain are listed. A code
that opens an empty screen is worse than a code that does not exist, so the
things requiring licensed feeds, a brokerage connection or a private network
are deliberately absent — see UNAVAILABLE below, which the MAIN directory shows
so the omission is visible rather than silent.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Function:
    code: str
    name: str
    category: str
    needs_ticker: bool
    summary: str


FUNCTIONS: tuple[Function, ...] = (
    # Navigation
    Function("MAIN", "Function Directory", "Navigation", False,
             "Every function this terminal provides, by category."),
    Function("HELP", "Keyboard & Commands", "Navigation", False,
             "Shortcuts and how the command line works."),
    Function("LAST", "Recent Screens", "Navigation", False,
             "The screens you have opened, most recent first."),
    Function("BLP", "Launchpad", "Navigation", False,
             "The nine-panel terminal canvas — the default view."),

    # Equities
    Function("DES", "Security Description", "Equities", True,
             "Company profile, exchange, employees, and key figures."),
    Function("FA", "Financial Analysis", "Equities", True,
             "Income statement, balance sheet and cash flow, five years."),
    Function("EE", "Earnings Estimates", "Equities", True,
             "Analyst consensus for coming earnings and revenue."),
    Function("ANR", "Analyst Recommendations", "Equities", True,
             "Buy/hold/sell distribution and price targets."),
    Function("HDS", "Holders", "Equities", True,
             "Largest institutional holders and their stakes."),
    Function("OMON", "Option Monitor", "Equities", True,
             "Option chain, implied volatility and the implied move."),
    Function("EQS", "Equity Screening", "Equities", False,
             "Filter the cached universe by fundamentals."),
    Function("SCAN", "Market Scanner", "Equities", False,
             "Whole-market scan for liquid movers, graded."),

    # Charts and technicals
    Function("GP", "Graph Price", "Charts", True,
             "Interactive price chart with drawing tools."),
    Function("PLAY", "Playbook", "Charts", False,
             "Entry, stop and targets for every watchlist ticker."),
    Function("SIG", "Signals", "Charts", False,
             "Setups that fired, with their measured record."),
    Function("IMAP", "Sector Map", "Charts", False,
             "Cap-weighted performance by sector, whole market."),
    Function("FACT", "Factor Study", "Charts", False,
             "Which confluence factors actually predict, measured."),
    Function("FWD", "Forward Test", "Charts", False,
             "What the engine's own plans did on bars it had not seen."),

    # News and macro
    Function("N", "News", "News", True,
             "Headlines for a ticker with AI summaries."),
    Function("NSE", "News Search", "News", False,
             "Search cached headlines across every ticker."),
    Function("ECO", "Economic Calendar", "Macro", False,
             "Upcoming releases with impact tiers."),
    Function("WB", "World Yields", "Macro", False,
             "Benchmark Treasury yields across maturities."),

    # Portfolio and tooling
    Function("PORT", "Portfolio", "Portfolio", False,
             "Watchlist exposure, weights and aggregate risk."),
    Function("ALRT", "Alerts", "Portfolio", False,
             "Your price alerts and delivered setups."),
    Function("CHAT", "Analyst", "Portfolio", False,
             "Ask questions across everything the terminal holds."),
)

BY_CODE = {f.code: f for f in FUNCTIONS}

CATEGORIES = ("Navigation", "Equities", "Charts", "News", "Macro", "Portfolio")

# Listed so the directory can say what is missing and why, rather than leaving
# a user to wonder whether they typed the code wrong.
UNAVAILABLE: tuple[tuple[str, str], ...] = (
    ("IB / MSGM", "Bloomberg's private messaging network between trading desks."),
    ("PEOP", "Licensed biographical database of finance executives."),
    ("EMSX", "Order routing to brokerages — this terminal places no orders."),
    ("BBXL", "Excel add-in built on Bloomberg's data licence."),
    ("M&A", "Licensed deal and transaction database."),
    ("CORP / GOVT / MTGE / MUNI", "Bond reference and pricing data is licensed."),
    ("YAS / FICM / SRCH", "Fixed-income analytics need that same bond data."),
    ("CRPR", "Moody's, S&P and Fitch ratings are licensed feeds."),
    ("AAL", "Earnings call audio and video rights."),
    ("WEAT", "Weather derivatives data."),
    ("GLCO / NRG", "Commodity futures curves need a futures data licence."),
)


def parse(command: str) -> tuple[str | None, str | None]:
    """Split a command line into (ticker, function code).

    Accepts Bloomberg's own order and the reverse, because people type both:
    `AAPL DES` and `DES AAPL` mean the same thing. A bare code with no ticker
    is valid for functions that do not need one; a bare ticker opens DES, which
    is what Bloomberg does.
    """
    parts = [p for p in (command or "").upper().replace("<GO>", "").split() if p]
    if not parts:
        return None, None
    if len(parts) == 1:
        token = parts[0]
        return (None, token) if token in BY_CODE else (token, "DES")

    first, second = parts[0], parts[1]
    if first in BY_CODE and second not in BY_CODE:
        return second, first
    if second in BY_CODE:
        return first, second
    return first, "DES"


def suggest(prefix: str, limit: int = 8) -> list[Function]:
    """Functions matching a typed prefix, code matches first."""
    query = (prefix or "").strip().upper()
    if not query:
        return list(FUNCTIONS[:limit])
    starts = [f for f in FUNCTIONS if f.code.startswith(query)]
    contains = [
        f for f in FUNCTIONS
        if f not in starts and (query in f.code or query in f.name.upper())
    ]
    return (starts + contains)[:limit]
