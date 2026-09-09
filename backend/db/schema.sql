CREATE TABLE IF NOT EXISTS watchlist (
    ticker     TEXT PRIMARY KEY,
    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sort_order INTEGER,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS price_cache (
    ticker      TEXT PRIMARY KEY,
    price       REAL,
    change_pct  REAL,
    volume      INTEGER,
    prev_close  REAL,
    currency    TEXT,
    fetched_at  TIMESTAMP,
    is_stale    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sparkline_cache (
    ticker      TEXT PRIMARY KEY,
    points_json TEXT,
    fetched_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    title        TEXT,
    publisher    TEXT,
    url          TEXT,
    published_at TIMESTAMP,
    summary      TEXT,
    ai_summary   TEXT,
    sentiment    TEXT,
    enrich_state TEXT NOT NULL DEFAULT 'pending',
    enrich_attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at   TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_news_ticker_published
    ON news(ticker, published_at DESC);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker         TEXT PRIMARY KEY,
    pe_ratio       REAL,
    forward_pe     REAL,
    market_cap     REAL,
    eps            REAL,
    revenue        REAL,
    sector         TEXT,
    industry       TEXT,
    dividend_yield REAL,
    beta           REAL,
    week52_high    REAL,
    week52_low     REAL,
    updated_at     TIMESTAMP,
    is_stale       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS universe (
    ticker     TEXT PRIMARY KEY,
    warm_state TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_tried TIMESTAMP
);

CREATE TABLE IF NOT EXISTS macro_events (
    id           TEXT PRIMARY KEY,
    release_id   INTEGER NOT NULL,
    release_name TEXT NOT NULL,
    event_date   TIMESTAMP NOT NULL,
    impact       TEXT NOT NULL,
    fetched_at   TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_events(event_date);

CREATE TABLE IF NOT EXISTS screener_presets (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    filter_json TEXT NOT NULL,
    created_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bars (
    ticker   TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts       TIMESTAMP NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, interval, ts)
);

CREATE TABLE IF NOT EXISTS earnings (
    ticker       TEXT NOT NULL,
    earnings_at  TIMESTAMP NOT NULL,
    eps_estimate REAL,
    fetched_at   TIMESTAMP,
    PRIMARY KEY (ticker, earnings_at)
);

CREATE TABLE IF NOT EXISTS signals (
    id           TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    grade        TEXT NOT NULL,
    score        INTEGER NOT NULL,
    factors_json TEXT NOT NULL,
    entry REAL, stop REAL, target1 REAL, target2 REAL,
    risk_reward  REAL NOT NULL,
    timeframes   TEXT NOT NULL,
    earnings_at  TIMESTAMP,
    created_at   TIMESTAMP,
    delivered_at TIMESTAMP,
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);

-- Measured performance per ticker per grade, replayed from cached daily bars.
-- Cached because a replay costs ~0.4s and the panel must not pay that per
-- render; refreshed by the poller on its signal-scan cycle.
CREATE TABLE IF NOT EXISTS backtest_stats (
    ticker      TEXT NOT NULL,
    grade       TEXT NOT NULL,
    signals     INTEGER NOT NULL,
    wins        INTEGER NOT NULL,
    losses      INTEGER NOT NULL,
    open_trades INTEGER NOT NULL,
    win_rate    REAL NOT NULL,
    avg_r       REAL NOT NULL,
    computed_at TIMESTAMP,
    PRIMARY KEY (ticker, grade)
);

-- The searchable symbol catalogue, pulled from the configured screener.
-- Separate from `universe`: that table is the screener's slow warm queue,
-- while this is ~20k names that exist only to be searched and picked.
CREATE TABLE IF NOT EXISTS symbols (
    symbol      TEXT PRIMARY KEY,   -- yfinance form (BRK-B)
    source_symbol   TEXT NOT NULL,      -- screener form (NYSE:BRK.B)
    name        TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- stock | fund | dr
    market_cap  REAL,
    fetched_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_cap ON symbols(market_cap DESC);

-- User-defined price alerts, distinct from engine-computed signals.
CREATE TABLE IF NOT EXISTS price_alerts (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT NOT NULL,
    direction    TEXT NOT NULL,     -- above | below
    price        REAL NOT NULL,
    note         TEXT,
    created_at   TIMESTAMP,
    triggered_at TIMESTAMP,
    delivered_at TIMESTAMP,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON price_alerts(triggered_at);

-- Settings editable from the UI. Values may be credentials, so nothing here
-- is echoed back to the browser in full.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP
);

-- Positions actually taken. Distinct from the watchlist, which is a list of
-- things to look at: this is a list of things you own, and the difference is
-- the difference between a research tool and a trading one.
--
-- The plan is copied in at entry rather than referenced, because plans are
-- recomputed continuously. Judging a trade against a plan the engine has since
-- revised would measure nothing.
CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT    NOT NULL,
    direction    TEXT    NOT NULL DEFAULT 'long',   -- long | short
    shares       REAL    NOT NULL,
    entry_price  REAL    NOT NULL,
    entry_at     TIMESTAMP NOT NULL,
    stop         REAL,
    target1      REAL,
    target2      REAL,
    plan_grade   TEXT,
    plan_score   INTEGER,
    plan_horizon TEXT,
    exit_price   REAL,
    exit_at      TIMESTAMP,
    exit_reason  TEXT,                              -- stop | target | manual | ...
    fees         REAL    NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(exit_at);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);

-- Forward test. Deliberately a separate table from `positions`, not a flag on
-- it: paper results must never be able to leak into a number the user reads as
-- their own money, and a WHERE clause is one forgotten predicate away from
-- doing exactly that.
--
-- The plan is frozen at arm time and never recomputed. That is the entire
-- point — an engine judged against a plan it has since revised measures
-- nothing.
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY,
    ticker        TEXT    NOT NULL,
    direction     TEXT    NOT NULL DEFAULT 'long',
    horizon       TEXT,
    interval      TEXT    NOT NULL,
    plan_grade    TEXT,
    plan_score    INTEGER,
    factors       TEXT,
    planned_entry REAL    NOT NULL,
    stop          REAL    NOT NULL,
    target1       REAL    NOT NULL,
    target2       REAL,
    armed_at      TIMESTAMP NOT NULL,
    entry_price   REAL,
    entry_at      TIMESTAMP,
    entry_gapped  INTEGER NOT NULL DEFAULT 0,
    exit_price    REAL,
    exit_at       TIMESTAMP,
    exit_reason   TEXT,
    exit_gapped   INTEGER NOT NULL DEFAULT 0,
    shares        REAL    NOT NULL DEFAULT 1,
    fees          REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paper_live ON paper_trades(exit_at, entry_at);
CREATE INDEX IF NOT EXISTS idx_paper_ticker ON paper_trades(ticker);
