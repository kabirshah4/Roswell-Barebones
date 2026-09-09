"""The only module in the project that issues SQL."""

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode connection, commit on clean exit, always close."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_MIGRATIONS = (
    # SQLite has no ADD COLUMN IF NOT EXISTS, and schema.sql is re-run on every
    # startup, so additive columns live here and are applied idempotently by
    # inspecting the existing table first.
    ("news", "link_status", "TEXT NOT NULL DEFAULT 'unchecked'"),
    ("news", "link_code", "INTEGER"),
    ("news", "link_checked_at", "TIMESTAMP"),
    ("news", "link_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("symbols", "sort_rank", "INTEGER NOT NULL DEFAULT 0"),
    ("fundamentals", "target_mean", "REAL"),
    ("fundamentals", "target_high", "REAL"),
    ("fundamentals", "target_low", "REAL"),
    ("fundamentals", "analyst_count", "REAL"),
    ("fundamentals", "peg_ratio", "REAL"),
    ("fundamentals", "price_to_book", "REAL"),
    ("fundamentals", "profit_margin", "REAL"),
    ("fundamentals", "gross_margin", "REAL"),
    ("fundamentals", "operating_margin", "REAL"),
    ("fundamentals", "return_on_equity", "REAL"),
    ("fundamentals", "debt_to_equity", "REAL"),
    ("fundamentals", "free_cashflow", "REAL"),
    ("fundamentals", "revenue_growth", "REAL"),
    ("fundamentals", "earnings_growth", "REAL"),
    ("fundamentals", "short_pct_float", "REAL"),
    ("fundamentals", "held_by_institutions", "REAL"),
    ("fundamentals", "avg_volume", "REAL"),
    ("fundamentals", "ma50", "REAL"),
    ("fundamentals", "ma200", "REAL"),
    ("fundamentals", "payout_ratio", "REAL"),
    ("fundamentals", "recommendation", "TEXT"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _apply_migrations(conn)


def add_watchlist_ticker(conn: sqlite3.Connection, ticker: str) -> None:
    """Raises sqlite3.IntegrityError if the ticker is already present.

    Non-atomic read-then-write: the SELECT below and the INSERT are two
    separate statements, not a transaction-guarded compare-and-swap. This
    was harmless when the only caller (``POST /api/watchlist``) blocked the
    whole event loop while checking the ticker upstream, since no other
    request could interleave. Now that route offloads the upstream check to
    a worker thread via ``asyncio.to_thread`` before calling this function,
    there is a genuine await point ahead of this SELECT/INSERT pair, so two
    concurrent adds could theoretically both read the same MAX(sort_order)
    and insert with the same sort_order. Left as-is deliberately: this is a
    single-user local tool, the worst case is two tickers sharing a
    sort_order, and ``list_watchlist`` already breaks ties by ticker name.
    """
    next_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO watchlist (ticker, added_at, sort_order) VALUES (?, ?, ?)",
        (ticker, _utc_now(), next_order),
    )


def list_watchlist(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM watchlist ORDER BY sort_order, ticker"
    ).fetchall()
    return [r["ticker"] for r in rows]


def remove_watchlist_ticker(conn: sqlite3.Connection, ticker: str) -> bool:
    cur = conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
    conn.execute("DELETE FROM price_cache WHERE ticker = ?", (ticker,))
    conn.execute("DELETE FROM sparkline_cache WHERE ticker = ?", (ticker,))
    return cur.rowcount > 0


def upsert_price(
    conn: sqlite3.Connection,
    ticker: str,
    price: float,
    prev_close: float,
    change_pct: float,
    volume: int,
    currency: str,
) -> None:
    """Insert or replace the single cached row for this ticker, clearing staleness."""
    conn.execute(
        """
        INSERT INTO price_cache
            (ticker, price, change_pct, volume, prev_close, currency, fetched_at, is_stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(ticker) DO UPDATE SET
            price      = excluded.price,
            change_pct = excluded.change_pct,
            volume     = excluded.volume,
            prev_close = excluded.prev_close,
            currency   = excluded.currency,
            fetched_at = excluded.fetched_at,
            is_stale   = 0
        """,
        (ticker, price, change_pct, volume, prev_close, currency, _utc_now()),
    )


def mark_stale(conn: sqlite3.Connection, tickers: Iterable[str]) -> None:
    """Flag rows as stale while leaving their last known values intact."""
    for ticker in tickers:
        conn.execute(
            "UPDATE price_cache SET is_stale = 1 WHERE ticker = ?", (ticker,)
        )


def get_prices(
    conn: sqlite3.Connection, tickers: Sequence[str] | None = None
) -> list[dict]:
    if tickers is None:
        rows = conn.execute(
            "SELECT * FROM price_cache ORDER BY ticker"
        ).fetchall()
    else:
        if not tickers:
            return []
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"SELECT * FROM price_cache WHERE ticker IN ({placeholders}) ORDER BY ticker",
            tuple(tickers),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_sparkline(
    conn: sqlite3.Connection, ticker: str, points: list[float]
) -> None:
    conn.execute(
        """
        INSERT INTO sparkline_cache (ticker, points_json, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            points_json = excluded.points_json,
            fetched_at  = excluded.fetched_at
        """,
        (ticker, json.dumps(points), _utc_now()),
    )


def get_sparkline(conn: sqlite3.Connection, ticker: str) -> list[float] | None:
    row = conn.execute(
        "SELECT points_json FROM sparkline_cache WHERE ticker = ?", (ticker,)
    ).fetchone()
    return json.loads(row["points_json"]) if row else None


def upsert_news_article(
    conn: sqlite3.Connection,
    article_id: str,
    ticker: str,
    title: str | None,
    publisher: str | None,
    url: str | None,
    published_at: str | None,
    summary: str | None,
) -> bool:
    """Insert an article if new. Returns True when newly inserted.

    Never touches ai_summary, sentiment, enrich_state, or enrich_attempts on an
    existing row — re-fetching a headline must not discard enrichment already paid
    for. An explicit existence check drives the return value, rather than inferring
    "new" from column state a caller could have changed.
    """
    existing = conn.execute(
        "SELECT 1 FROM news WHERE id = ?", (article_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE news SET title = ?, publisher = ?, url = ?, fetched_at = ?
            WHERE id = ?
            """,
            (title, publisher, url, _utc_now(), article_id),
        )
        return False
    conn.execute(
        """
        INSERT INTO news
            (id, ticker, title, publisher, url, published_at, summary, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (article_id, ticker, title, publisher, url, published_at, summary, _utc_now()),
    )
    return True


def list_news(
    conn: sqlite3.Connection, ticker: str, limit: int = 20
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM news WHERE ticker = ?
        ORDER BY published_at DESC LIMIT ?
        """,
        (ticker, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_enrichments(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, ticker, title, summary FROM news
        WHERE enrich_state = 'pending'
        ORDER BY published_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_enrichment(
    conn: sqlite3.Connection, article_id: str, ai_summary: str, sentiment: str
) -> None:
    conn.execute(
        """
        UPDATE news SET ai_summary = ?, sentiment = ?, enrich_state = 'done'
        WHERE id = ?
        """,
        (ai_summary, sentiment, article_id),
    )


def mark_enrich_failed(conn: sqlite3.Connection, article_id: str) -> None:
    conn.execute(
        "UPDATE news SET enrich_state = 'failed' WHERE id = ?", (article_id,)
    )


def mark_enrich_skipped(conn: sqlite3.Connection, article_id: str) -> None:
    conn.execute(
        "UPDATE news SET enrich_state = 'skipped' WHERE id = ?", (article_id,)
    )


def bump_enrich_attempts(conn: sqlite3.Connection, article_id: str) -> int:
    """Increment the retry counter and return the new value."""
    conn.execute(
        "UPDATE news SET enrich_attempts = enrich_attempts + 1 WHERE id = ?",
        (article_id,),
    )
    row = conn.execute(
        "SELECT enrich_attempts FROM news WHERE id = ?", (article_id,)
    ).fetchone()
    return int(row["enrich_attempts"]) if row else 0


def reset_skipped_to_pending(conn: sqlite3.Connection) -> int:
    """Re-queue articles skipped while no API key was configured."""
    cur = conn.execute(
        "UPDATE news SET enrich_state = 'pending' WHERE enrich_state = 'skipped'"
    )
    return cur.rowcount


_FUNDAMENTAL_FIELDS = (
    "pe_ratio", "forward_pe", "market_cap", "eps", "revenue", "sector",
    "industry", "dividend_yield", "beta", "week52_high", "week52_low",
    "target_mean",
    "target_high",
    "target_low",
    "analyst_count",
    "peg_ratio",
    "price_to_book",
    "profit_margin",
    "gross_margin",
    "operating_margin",
    "return_on_equity",
    "debt_to_equity",
    "free_cashflow",
    "revenue_growth",
    "earnings_growth",
    "short_pct_float",
    "held_by_institutions",
    "avg_volume",
    "ma50",
    "ma200",
    "payout_ratio",
    "recommendation",
)


def upsert_fundamentals(conn: sqlite3.Connection, ticker: str, **fields) -> None:
    values = {name: fields.get(name) for name in _FUNDAMENTAL_FIELDS}
    columns = ", ".join(_FUNDAMENTAL_FIELDS)
    placeholders = ", ".join("?" * len(_FUNDAMENTAL_FIELDS))
    updates = ", ".join(f"{n} = excluded.{n}" for n in _FUNDAMENTAL_FIELDS)
    conn.execute(
        f"""
        INSERT INTO fundamentals (ticker, {columns}, updated_at, is_stale)
        VALUES (?, {placeholders}, ?, 0)
        ON CONFLICT(ticker) DO UPDATE SET
            {updates}, updated_at = excluded.updated_at, is_stale = 0
        """,
        (ticker, *[values[n] for n in _FUNDAMENTAL_FIELDS], _utc_now()),
    )


def get_fundamentals(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM fundamentals WHERE ticker = ?", (ticker,)
    ).fetchone()
    return dict(row) if row else None


def seed_universe(conn: sqlite3.Connection, tickers: list[str]) -> int:
    """Insert any tickers not already present. Idempotent.

    Uses INSERT OR IGNORE so re-seeding on every startup never resets the
    warm_state of tickers already fetched.
    """
    added = 0
    for ticker in tickers:
        cur = conn.execute(
            "INSERT OR IGNORE INTO universe (ticker) VALUES (?)", (ticker,)
        )
        added += cur.rowcount
    return added


def pending_universe(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM universe WHERE warm_state = 'pending' ORDER BY ticker LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["ticker"] for r in rows]


def mark_universe_done(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "UPDATE universe SET warm_state = 'done', last_tried = ? WHERE ticker = ?",
        (_utc_now(), ticker),
    )


def mark_universe_failed(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "UPDATE universe SET warm_state = 'failed', last_tried = ? WHERE ticker = ?",
        (_utc_now(), ticker),
    )


def bump_universe_attempts(conn: sqlite3.Connection, ticker: str) -> int:
    conn.execute(
        "UPDATE universe SET attempts = attempts + 1, last_tried = ? WHERE ticker = ?",
        (_utc_now(), ticker),
    )
    row = conn.execute(
        "SELECT attempts FROM universe WHERE ticker = ?", (ticker,)
    ).fetchone()
    return int(row["attempts"]) if row else 0


def universe_coverage(conn: sqlite3.Connection) -> dict:
    """Report how much of the universe is actually screenable.

    `screened` counts universe tickers that have a fundamentals row — not
    merely a 'done' flag — because a row without fundamentals cannot match
    any filter and must not be counted as covered.
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM universe").fetchone()["n"]
    screened = conn.execute(
        """
        SELECT COUNT(*) AS n FROM universe u
        JOIN fundamentals f ON f.ticker = u.ticker
        """
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM universe WHERE warm_state = 'pending'"
    ).fetchone()["n"]
    return {"screened": screened, "universe": total, "warming": pending > 0}


def upsert_macro_event(
    conn: sqlite3.Connection,
    event_id: str,
    release_id: int,
    release_name: str,
    event_date: str,
    impact: str,
) -> None:
    conn.execute(
        """
        INSERT INTO macro_events
            (id, release_id, release_name, event_date, impact, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            release_name = excluded.release_name,
            event_date   = excluded.event_date,
            impact       = excluded.impact,
            fetched_at   = excluded.fetched_at
        """,
        (event_id, release_id, release_name, event_date, impact, _utc_now()),
    )


def list_macro_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """Upcoming events only, soonest first."""
    rows = conn.execute(
        """
        SELECT * FROM macro_events
        WHERE event_date >= date('now')
        ORDER BY event_date ASC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_preset(conn: sqlite3.Connection, name: str, filter_json: str) -> None:
    conn.execute(
        """
        INSERT INTO screener_presets (name, filter_json, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET filter_json = excluded.filter_json
        """,
        (name, filter_json, _utc_now()),
    )


def list_presets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT name, filter_json, created_at FROM screener_presets ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_preset(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM screener_presets WHERE name = ?", (name,))
    return cur.rowcount > 0


def upsert_bars(
    conn: sqlite3.Connection, ticker: str, interval: str, rows: list[dict]
) -> int:
    """Insert or correct OHLCV bars. Returns the number written.

    Re-fetching an existing bar overwrites it: the most recent candle is often
    still forming when first stored, so a later fetch is the more accurate one.
    """
    written = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO bars (ticker, interval, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, interval, ts) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume
            """,
            (ticker, interval, r["ts"], r["open"], r["high"], r["low"],
             r["close"], r["volume"]),
        )
        written += 1
    return written


def get_bars(
    conn: sqlite3.Connection, ticker: str, interval: str, limit: int = 1000
) -> list[dict]:
    """Return up to `limit` most recent bars, oldest-first.

    Indicators need chronological order, but the useful slice is the recent
    tail, so this selects newest-first then reverses.
    """
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume FROM bars
        WHERE ticker = ? AND interval = ?
        ORDER BY ts DESC LIMIT ?
        """,
        (ticker, interval, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def upsert_earnings(
    conn: sqlite3.Connection,
    ticker: str,
    earnings_at: str,
    eps_estimate: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO earnings (ticker, earnings_at, eps_estimate, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, earnings_at) DO UPDATE SET
            eps_estimate = excluded.eps_estimate,
            fetched_at   = excluded.fetched_at
        """,
        (ticker, earnings_at, eps_estimate, _utc_now()),
    )


def next_earnings(
    conn: sqlite3.Connection, ticker: str, after_iso: str
) -> str | None:
    """Soonest earnings date strictly after `after_iso`, or None if unknown."""
    row = conn.execute(
        """
        SELECT earnings_at FROM earnings
        WHERE ticker = ? AND earnings_at > ?
        ORDER BY earnings_at ASC LIMIT 1
        """,
        (ticker, after_iso),
    ).fetchone()
    return row["earnings_at"] if row else None


def upsert_signal(conn: sqlite3.Connection, setup: object, fingerprint: str) -> bool:
    """Persist a setup. Returns True only when newly inserted.

    The fingerprint primary key is what stops a persistent chart condition
    re-alerting on every poll cycle.
    """
    import json as _json

    existing = conn.execute(
        "SELECT 1 FROM signals WHERE id = ?", (fingerprint,)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT INTO signals
            (id, ticker, direction, grade, score, factors_json, entry, stop,
             target1, target2, risk_reward, timeframes, earnings_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fingerprint, setup.ticker, setup.direction, setup.grade, setup.score,
            _json.dumps(setup.factors), setup.entry, setup.stop, setup.target1,
            setup.target2, setup.risk_reward, setup.timeframes, setup.earnings_at,
            _utc_now(),
        ),
    )
    return True


def list_signals(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def undelivered_signals(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM signals
        WHERE delivered_at IS NULL AND attempts < 3
        ORDER BY created_at ASC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_signal_delivered(conn: sqlite3.Connection, signal_id: str) -> None:
    conn.execute(
        "UPDATE signals SET delivered_at = ? WHERE id = ?", (_utc_now(), signal_id)
    )


def bump_signal_attempts(conn: sqlite3.Connection, signal_id: str) -> None:
    conn.execute(
        "UPDATE signals SET attempts = attempts + 1 WHERE id = ?", (signal_id,)
    )


def unchecked_links(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Articles whose URL has not been validated yet."""
    rows = conn.execute(
        """
        SELECT id, ticker, title, url FROM news
        WHERE link_status = 'unchecked' AND url IS NOT NULL AND url != ''
        ORDER BY published_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_link_status(
    conn: sqlite3.Connection, article_id: str, status: str, code: int | None
) -> None:
    conn.execute(
        """
        UPDATE news SET link_status = ?, link_code = ?, link_checked_at = ?
        WHERE id = ?
        """,
        (status, code, _utc_now(), article_id),
    )


def bump_link_attempts(conn: sqlite3.Connection, article_id: str) -> int:
    """Record an inconclusive check. Returns the new attempt count.

    The row stays `unchecked` so it is retried; only the caller decides when
    enough attempts have gone by to call it broken.
    """
    conn.execute(
        "UPDATE news SET link_attempts = link_attempts + 1, link_checked_at = ? "
        "WHERE id = ?",
        (_utc_now(), article_id),
    )
    row = conn.execute(
        "SELECT link_attempts FROM news WHERE id = ?", (article_id,)
    ).fetchone()
    return int(row["link_attempts"]) if row else 0


def broken_links(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Articles whose URL failed validation, newest first."""
    rows = conn.execute(
        """
        SELECT id, ticker, title, url, link_status, link_code, link_checked_at
        FROM news
        WHERE link_status IN ('dead', 'error')
        ORDER BY published_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def link_summary(conn: sqlite3.Connection) -> dict:
    """Counts per link status, so the UI can report coverage honestly."""
    rows = conn.execute(
        "SELECT link_status, COUNT(*) AS n FROM news GROUP BY link_status"
    ).fetchall()
    return {r["link_status"]: r["n"] for r in rows}


def upsert_backtest_stats(conn: sqlite3.Connection, ticker: str, stats: dict) -> int:
    """Store a replay's per-grade results. Returns the number of grades written."""
    written = 0
    for grade, s in stats.items():
        values = s if isinstance(s, dict) else vars(s)
        conn.execute(
            """
            INSERT INTO backtest_stats
                (ticker, grade, signals, wins, losses, open_trades,
                 win_rate, avg_r, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, grade) DO UPDATE SET
                signals = excluded.signals, wins = excluded.wins,
                losses = excluded.losses, open_trades = excluded.open_trades,
                win_rate = excluded.win_rate, avg_r = excluded.avg_r,
                computed_at = excluded.computed_at
            """,
            (ticker, grade, values["signals"], values["wins"], values["losses"],
             values["open_trades"], values["win_rate"], values["avg_r"],
             _utc_now()),
        )
        written += 1
    return written


def get_backtest_stats(conn: sqlite3.Connection, ticker: str) -> dict:
    """Cached per-grade results for one ticker, keyed by grade."""
    rows = conn.execute(
        "SELECT * FROM backtest_stats WHERE ticker = ?", (ticker,)
    ).fetchall()
    return {r["grade"]: dict(r) for r in rows}


def all_backtest_stats(conn: sqlite3.Connection) -> dict:
    """Every cached result, as {ticker: {grade: stats}}."""
    out: dict = {}
    for r in conn.execute("SELECT * FROM backtest_stats"):
        out.setdefault(r["ticker"], {})[r["grade"]] = dict(r)
    return out


# --- symbol catalogue -------------------------------------------------------

def replace_symbols(conn: sqlite3.Connection, symbols: list) -> int:
    """Replace the catalogue wholesale. Returns the number stored.

    Replaced rather than merged: a delisted name should disappear, and the
    whole list arrives in one response anyway. Done in a single transaction so
    a failure mid-write cannot leave an empty catalogue.
    """
    now = _utc_now()
    rows = [
        (s.symbol, s.source_symbol, s.name, s.exchange, s.kind, s.market_cap,
         getattr(s, "sort_rank", 0), now)
        for s in symbols
    ]
    if not rows:
        return 0
    conn.execute("DELETE FROM symbols")
    conn.executemany(
        "INSERT OR REPLACE INTO symbols "
        "(symbol, source_symbol, name, exchange, kind, market_cap, sort_rank, "
        " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def search_symbols(
    conn: sqlite3.Connection, query: str, limit: int = 20, kind: str | None = None
) -> list[dict]:
    """Symbols matching a query, best match first.

    Ordering is deliberate: an exact ticker match, then tickers starting with
    the query, then names containing it -- and largest company first within
    each tier. Typing "AA" should surface AA and AAPL, not an obscure fund
    whose description happens to contain the letters.
    """
    q = (query or "").strip().upper()
    if not q:
        return []
    like = f"{q}%"
    contains = f"%{q}%"
    kind_clause = " AND kind = ?" if kind else ""
    params: list = [q, like, like, contains, contains]
    if kind:
        params.append(kind)
    params.append(max(1, min(int(limit), 100)))
    rows = conn.execute(
        f"""
        SELECT symbol, source_symbol, name, exchange, kind, market_cap, sort_rank,
               CASE
                   WHEN symbol = ?        THEN 0
                   WHEN symbol LIKE ?     THEN 1
                   WHEN UPPER(name) LIKE ? THEN 2
                   ELSE 3
               END AS rank
        FROM symbols
        WHERE (symbol LIKE ? OR UPPER(name) LIKE ?){kind_clause}
        ORDER BY rank ASC, sort_rank DESC, market_cap DESC NULLS LAST, symbol ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def browse_symbols(
    conn: sqlite3.Connection,
    offset: int = 0,
    limit: int = 50,
    kind: str | None = None,
) -> list[dict]:
    """A page of the whole catalogue, largest company first.

    Exists so clicking the empty search box can show the catalogue rather than
    demanding you already know what you are looking for.

    The sort ends with `symbol ASC` on purpose: market cap alone is not a total
    order — thousands of rows share a NULL cap, and SQLite is free to return
    ties in any order between queries. Without the tiebreak, paging through the
    list would silently repeat and skip rows.
    """
    where = "WHERE kind = ?" if kind else ""
    params: list = [kind] if kind else []
    params += [max(1, min(int(limit), 200)), max(0, int(offset))]
    rows = conn.execute(
        f"""
        SELECT symbol, source_symbol, name, exchange, kind, market_cap, sort_rank
        FROM symbols {where}
        ORDER BY sort_rank DESC, market_cap DESC NULLS LAST, symbol ASC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def symbol_count(conn: sqlite3.Connection, kind: str | None = None) -> int:
    if kind:
        row = conn.execute(
            "SELECT COUNT(*) c FROM symbols WHERE kind = ?", (kind,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()
    return int(row["c"])


def get_symbol(conn: sqlite3.Connection, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM symbols WHERE symbol = ?", (symbol.strip().upper(),)
    ).fetchone()
    return dict(row) if row else None


# --- user price alerts ------------------------------------------------------

def add_price_alert(
    conn: sqlite3.Connection, ticker: str, direction: str, price: float,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO price_alerts (ticker, direction, price, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker.strip().upper(), direction, float(price), note, _utc_now()),
    )
    return int(cur.lastrowid)


def list_price_alerts(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM price_alerts ORDER BY triggered_at IS NOT NULL, "
        "created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_price_alert(conn: sqlite3.Connection, alert_id: int) -> bool:
    cur = conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
    return cur.rowcount > 0


def armed_price_alerts(conn: sqlite3.Connection) -> list[dict]:
    """Alerts that have not fired yet."""
    rows = conn.execute(
        "SELECT * FROM price_alerts WHERE triggered_at IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_triggered(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE price_alerts SET triggered_at = ? WHERE id = ? "
        "AND triggered_at IS NULL",
        (_utc_now(), alert_id),
    )


def undelivered_price_alerts(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM price_alerts WHERE triggered_at IS NOT NULL "
        "AND delivered_at IS NULL AND attempts < 3 ORDER BY triggered_at ASC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_delivered(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE price_alerts SET delivered_at = ? WHERE id = ?",
        (_utc_now(), alert_id),
    )


def bump_alert_attempts(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute(
        "UPDATE price_alerts SET attempts = attempts + 1 WHERE id = ?", (alert_id,)
    )


# --- settings ---------------------------------------------------------------

def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, _utc_now()),
    )


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def delete_setting(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0


# --- positions ---------------------------------------------------------------

def add_position(conn: sqlite3.Connection, **fields) -> int:
    """Open a position. The plan levels are copied in, not referenced."""
    columns = ("ticker", "direction", "shares", "entry_price", "entry_at",
               "stop", "target1", "target2", "plan_grade", "plan_score",
               "plan_horizon", "fees", "note")
    values = {k: fields.get(k) for k in columns}
    values["ticker"] = str(values["ticker"]).strip().upper()
    values["direction"] = values["direction"] or "long"
    values["entry_at"] = values["entry_at"] or _utc_now()
    values["fees"] = values["fees"] or 0
    cur = conn.execute(
        f"INSERT INTO positions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        tuple(values[k] for k in columns),
    )
    return int(cur.lastrowid)


def list_positions(
    conn: sqlite3.Connection, *, open_only: bool = False, limit: int = 500
) -> list[dict]:
    """Open first, then most recently closed — the ones needing a decision on
    top."""
    where = "WHERE exit_at IS NULL" if open_only else ""
    rows = conn.execute(
        f"SELECT * FROM positions {where} "
        f"ORDER BY exit_at IS NOT NULL, COALESCE(exit_at, entry_at) DESC "
        f"LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_position(conn: sqlite3.Connection, position_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    return dict(row) if row else None


def close_position(
    conn: sqlite3.Connection, position_id: int, exit_price: float,
    exit_reason: str | None = None, exit_at: str | None = None,
    fees: float | None = None,
) -> bool:
    """Record the exit. Refuses to close an already-closed position: a second
    exit price would silently overwrite the result the journal measured."""
    row = get_position(conn, position_id)
    if row is None or row["exit_at"] is not None:
        return False
    conn.execute(
        "UPDATE positions SET exit_price = ?, exit_at = ?, exit_reason = ?, "
        "fees = ? WHERE id = ?",
        (float(exit_price), exit_at or _utc_now(), exit_reason,
         (row["fees"] or 0) + (fees or 0), position_id),
    )
    return True


def update_position(
    conn: sqlite3.Connection, position_id: int, **fields
) -> bool:
    """Adjust an open position. Only the fields a trader legitimately revises:
    the stop moves, the note grows. Entry price and share count do not, because
    changing them rewrites history the journal already measured."""
    allowed = {"stop", "target1", "target2", "note"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return False
    assignments = ", ".join(f"{k} = ?" for k in changes)
    cur = conn.execute(
        f"UPDATE positions SET {assignments} WHERE id = ? AND exit_at IS NULL",
        (*changes.values(), position_id),
    )
    return cur.rowcount > 0


def delete_position(conn: sqlite3.Connection, position_id: int) -> bool:
    cur = conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
    return cur.rowcount > 0


# --- paper trades (the forward test) -----------------------------------------

def arm_paper_trade(conn: sqlite3.Connection, **fields) -> int:
    columns = ("ticker", "direction", "horizon", "interval", "plan_grade",
               "plan_score", "factors", "planned_entry", "stop", "target1",
               "target2", "armed_at", "shares")
    values = {k: fields.get(k) for k in columns}
    values["ticker"] = str(values["ticker"]).strip().upper()
    values["direction"] = values["direction"] or "long"
    values["armed_at"] = values["armed_at"] or _utc_now()
    values["shares"] = values["shares"] or 1
    cur = conn.execute(
        f"INSERT INTO paper_trades ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        tuple(values[k] for k in columns),
    )
    return int(cur.lastrowid)


def paper_trades(conn: sqlite3.Connection, limit: int = 1000) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_trades "
        "ORDER BY exit_at IS NOT NULL, COALESCE(exit_at, armed_at) DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def live_paper_trades(conn: sqlite3.Connection) -> list[dict]:
    """Armed or open — everything the runner still has work to do on."""
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE exit_at IS NULL ORDER BY armed_at"
    ).fetchall()
    return [dict(r) for r in rows]


def has_live_paper_trade(
    conn: sqlite3.Connection, ticker: str, horizon: str | None
) -> bool:
    """One at a time per ticker and horizon.

    Without this, a setup that stays valid for a week arms a fresh trade every
    cycle and the record fills with fifty copies of one idea.
    """
    row = conn.execute(
        "SELECT 1 FROM paper_trades WHERE ticker = ? AND exit_at IS NULL "
        "AND (horizon IS ? OR horizon = ?) LIMIT 1",
        (ticker.strip().upper(), horizon, horizon),
    ).fetchone()
    return row is not None


def fill_paper_trade(conn: sqlite3.Connection, trade_id: int, price: float,
                     at: str, gapped: bool) -> None:
    conn.execute(
        "UPDATE paper_trades SET entry_price = ?, entry_at = ?, "
        "entry_gapped = ? WHERE id = ? AND entry_at IS NULL",
        (float(price), at, 1 if gapped else 0, trade_id),
    )


def close_paper_trade(conn: sqlite3.Connection, trade_id: int,
                      price: float | None, at: str, reason: str,
                      gapped: bool = False) -> None:
    conn.execute(
        "UPDATE paper_trades SET exit_price = ?, exit_at = ?, exit_reason = ?, "
        "exit_gapped = ? WHERE id = ? AND exit_at IS NULL",
        (price if price is None else float(price), at, reason,
         1 if gapped else 0, trade_id),
    )


def newest_bar_timestamps(conn: sqlite3.Connection, ticker: str) -> dict:
    """The most recent bar per interval, for freshness checks."""
    rows = conn.execute(
        "SELECT interval, MAX(ts) AS newest FROM bars WHERE ticker = ? "
        "GROUP BY interval",
        (ticker.strip().upper(),),
    ).fetchall()
    return {r["interval"]: r["newest"] for r in rows}
