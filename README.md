```
╦═╗╔═╗╔═╗╦ ╦╔═╗╦  ╦  
╠╦╝║ ║╚═╗║║║║╣ ║  ║  
╩╚═╚═╝╚═╝╚╩╝╚═╝╩═╝╩═╝
```

# Roswell

**A Bloomberg-style market research terminal that runs entirely on your own machine.**

[![CI](https://github.com/kabirshah4/Roswell-Terminal/actions/workflows/ci.yml/badge.svg)](https://github.com/kabirshah4/Roswell-Terminal/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-blue)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-informational)
![License](https://img.shields.io/badge/license-MIT-black)
![No build step](https://img.shields.io/badge/frontend-no%20build%20step-lightgrey)

Nine panels on a draggable grid, a command line you navigate by typing function
codes, a signal engine that computes entries and stops from cached bars, and an
optional AI analyst that can read all of it. No account, no hosted backend, no
subscription — one FastAPI process, one SQLite file, one browser tab.

| Panel | What it does |
|---|---|
| Watchlist | Live prices, change, and a sparkline per ticker; searchable symbol browser |
| Chart | Embedded TradingView chart, with a deep link to your own |
| Fundamentals | 30+ metrics across valuation, profitability, growth, analyst targets and positioning |
| News | Headlines with AI summaries, sentiment, and dead-link flags |
| Screener | Filters a cached S&P 500 universe; saved screens |
| Macro | Upcoming economic releases, impact-tiered |
| Alerts | Fired setups and your own price alerts |
| Playbook | Entry, stop and targets for every ticker, by horizon |
| Scanner | Whole-market scan for liquid movers, graded with entry/stop/targets |
| Analyst | Chat over everything above, including price projections |

---

## Screenshots

<!-- Add images to docs/screenshots/ and link them here, e.g.
![The MARKET tab](docs/screenshots/market.png)
-->

*Not yet captured — run the app and drop a few PNGs into `docs/screenshots/`.*

---

## Getting started

### 1. Install

Roswell runs on **macOS, Linux and Windows**. It needs **Python 3.12+** and
[uv](https://docs.astral.sh/uv/) — uv installs the right Python for you, so
that is the only prerequisite.

macOS and Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then clone and install. `uv sync` creates the virtualenv and installs
everything from `uv.lock`:

```bash
git clone https://github.com/kabirshah4/Roswell-Terminal.git
cd Roswell-Terminal
uv sync
```

> **Every command from here runs inside the project directory.** `uv` looks for
> `pyproject.toml` in the working directory and its parents, so running from
> your home folder fails with `No pyproject.toml found`.

Every command below is identical on all three platforms — `uv run` handles the
virtualenv, so there is nothing to activate and no `python` vs `python3`
difference to worry about.

### 2. Run

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

**No API keys are required to start.** The terminal boots, the watchlist polls
live prices, charts render, the signal engine computes entries and stops, and
every feature that needs a key says which key it needs instead of failing
silently.

To run it as a native desktop window instead — it picks a free port and falls
back to your browser if `pywebview` is not installed:

```bash
uv run python desktop.py
```

Stop the server with `Ctrl-C`.

### 3. Where the API keys go

Copy the template and edit it. **`.env` belongs in the project root, beside
`pyproject.toml`:**

```bash
cp .env.example .env
```

```
Roswell-Terminal/
├── .env             ← here
├── pyproject.toml
├── backend/
└── frontend/
```

The app reads `.env` at startup by itself — there is no need to `source` or
`export` anything first. **Restart the server after editing it.** A variable
already exported in your shell always wins over the file, so a temporary
override is never clobbered.

Every key is optional. This is the whole list:

| Variable | Unlocks | Without it | Get one |
|---|---|---|---|
| `GEMINI_API_KEY` | AI news summaries + analyst chat | Both features report themselves off | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free tier |
| `ANTHROPIC_API_KEY` | Same two features, as an alternative | Falls back to Gemini, or off | [console.anthropic.com](https://console.anthropic.com/settings/keys) — bills per call |
| `FRED_API_KEY` | Macro calendar (`ECO`) and world yields (`WB`) | Calendar is empty | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) — free |
| `DISCORD_WEBHOOK_URL` | Pushes triggered setups and price alerts to a channel | Alerts still fire in the UI, just not pushed | Discord → Channel Settings → Integrations → Webhooks |
| `SCREENER_SCAN_URL` | Symbol search, the Scanner panel, the `IMAP` sector map | Those three say "no screener source configured" | See [Bring your own screener](#bring-your-own-screener) |
| `ROSWELL_ALLOWED_HOSTS` | Reaching the terminal from another device | Only needed for non-localhost hostnames | See the *Locking the terminal* reference below |

If you set both `GEMINI_API_KEY` and `ANTHROPIC_API_KEY`, Gemini is preferred —
news enrichment fires on every new article and chat spends a request per
lookup, and Gemini has a free tier where Anthropic bills per call. Force one
with `chat_provider` in `backend/config.py`.

To confirm what the app actually picked up, check `/api/health` — it reports
`ai_enabled`, `fred_enabled`, `alerts_enabled` and `chat_enabled`:

```bash
curl -s http://127.0.0.1:8000/api/health
```

### 4. First steps in the terminal

- **Add tickers.** Click the add box on the Watchlist panel and type a symbol.
  Symbols are validated against Yahoo before being stored, so a typo is
  rejected rather than saved as a dead row. Full-text search over a symbol
  catalogue needs `SCREENER_SCAN_URL`; typing an exact ticker always works.
- **Navigate by function code.** Type into the command line at the top:
  `AAPL DES` for a company description, `AAPL FA` for financials, a bare
  `MSFT` to open its description, or `MAIN` for the directory of every code.
- **Switch tabs** with `1`–`5`, and press `?` for the full shortcut list.
- **Your data lives in `terminal.db`** in the project root — watchlist, cached
  bars, news and settings. It is gitignored. Delete it to start clean.

### 5. Tests

```bash
uv run pytest
```

1,244 offline tests, about 50 seconds, no network required. To exercise the
real Yahoo API as well:

```bash
uv run pytest -m live
```

Run the live suite when quotes stop updating — it distinguishes an upstream
`yfinance` break from a bug in this app.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `error: No pyproject.toml found` | You are not in the project directory. `cd Roswell-Terminal` first. |
| `Failed to spawn: uvicorn` | Dependencies are not installed yet — run `uv sync`, and start the server with `uv run` so it uses the project venv. |
| `sh: #: No such file or directory` | Your shell does not treat `#` as a comment when pasted. Paste commands without trailing comments. |
| `Address already in use` | Something already holds port 8000. Use `--port 8001`, or stop the other process. |
| A panel says a key is missing | That is by design. Add the key from the table above and restart. |

---

## How it is built

The constraints below are the interesting part of the project; most of them are
enforced by tests rather than convention.

**Read routes never touch the network.** Exactly one module,
`backend/services/price_poller.py`, performs outbound I/O on a cadence; every
read route serves SQLite. Outbound API volume therefore depends on the poll
interval and watchlist size alone — not on how many browser tabs are open. The
single exception is `POST /api/watchlist`, which validates a symbol before
storing it. Tests assert this by injecting clients that raise on any fetch.

**The unofficial dependency lives in one file.**
`backend/services/yfinance_client.py` is the only module that imports
`yfinance`. That library is unofficial and will eventually break; this is the
one place to repair it. Only five modules may import `httpx` at all.

**The signal engine does no I/O whatsoever.** `indicators.py`, `signal_engine.py`
and `forecast.py` are pure functions over price frames. That is precisely what
makes the backtest possible: the same code that fires a live signal can be
replayed over history without touching the network — so the backtest measures
the real engine, not a reimplementation of it.

**Stale data is refused, not served.** Rather than computing confident-looking
entries and stops from yesterday's intraday bars, the engine detects staleness
and declines. A level that is quietly wrong is worse than a level that is
absent.

**The grades are checked against outcomes.** The engine scores seven confluence
factors across three timeframes, and the `FACT` screen replays them over history
to report which factors actually carried predictive weight — including the ones
that did not. A grading system nobody has validated is a horoscope.

**The layout maths is chosen, not accidental.** Panel geometry is stored in
24-unit lattice coordinates rather than pixels, so an arrangement survives a
resized window. 24 is used because its divisors (1, 2, 3, 4, 6) cover every
offered custom grid, meaning generated slots always land on whole units with
nothing left over. All 25 grid combinations are asserted to tile completely by
running the real generator.

**It degrades honestly.** With no credentials at all the app boots, serves every
panel, and each disabled feature names the variable it wants. Nothing fails
silently and nothing pretends to have data it does not have.

### Layout

```
backend/
  main.py            app wiring, startup, health
  config.py          intervals and paths
  routes/            HTTP surface — reads SQLite, never fetches
  services/          poller, signal engine, forecast, screener client, AI agents
  db/                schema.sql + query layer
frontend/            vanilla JS, no build step
tests/               1,244 offline tests
```

No bundler, no framework, no `node_modules` — `frontend/` is three files served
as static assets.

---

## Bring your own screener

Three features are powered by a market screener: the searchable symbol
catalogue, the whole-market Scanner panel, and the `IMAP` sector map. **Roswell
ships without a screener endpoint**, and those three degrade to a clear "no
screener source configured" state until you provide one.

Set `SCREENER_SCAN_URL` in `.env` to any endpoint that accepts a JSON `POST`:

```jsonc
{
  "filter":  [{"left": "type", "operation": "equal", "right": "stock"}],
  "columns": ["name", "description", "exchange", "type", "market_cap_basic"],
  "range":   [0, 25000],
  "sort":    {"sortBy": "market_cap_basic", "sortOrder": "desc"}
}
```

and answers with:

```jsonc
{"data": [{"s": "NASDAQ:AAPL", "d": ["AAPL", "Apple Inc.", "NASDAQ", "stock", 3.0e12]}]}
```

`{market}` in the URL is substituted per market (`america`, `crypto`, `forex`);
a URL without the placeholder is used unchanged, which is what a single-market
screener wants. Everything else — parsing, deduplication, and the symbol
conversion to the form yfinance quotes — is already implemented in
`backend/services/screener_client.py`.

The ten curated indices (`^GSPC`, `^VIX`, and friends) are hardcoded and work
with no screener configured at all.

---

## Reference

Everything below documents a specific screen or subsystem. Expand what you need.

<details>
<summary><b>The command line</b></summary>

Bloomberg navigates by typing a short code rather than hunting a menu. That is
the part worth copying: it is fast, memorable, and scales past what a menu bar
can hold. Type into the command line at the top:

```
AAPL DES     company description        FA    financial statements
AAPL FA      either order works         ANR   analyst ratings & targets
MSFT         a bare ticker opens DES    EE    earnings estimates
MAIN         the function directory     HDS   institutional holders
IMAP         cap-weighted sector map    PORT  watchlist exposure
NSE          search all cached news     LAST  recent screens
```

</details>

<details>
<summary><b>Panels</b></summary>

Every panel is dragged and resized on its own. Grab the title bar to move it,
pull any of eight handles to resize, and edges snap to a 24x24 lattice so one
panel's edge lands where its neighbour's does. Arrow keys move a focused panel;
alt-arrows resize it.

Geometry is stored in lattice units, not pixels, so an arrangement survives a
resized window — and it is saved per tab, since a layout dragged for RESEARCH
should not follow you to TRADE. RESET LAYOUT restores the default for the
current tab only.

</details>

<details>
<summary><b>The five tabs</b></summary>

Nine panels and a dozen screens on one canvas is more than anyone reads at once.
Each tab is a job, and shows only what that job needs, arranged for it. Press
`1`–`5` or click.

| Tab | Shows | For |
|---|---|---|
| **MARKET** | watchlist, chart, scanner, macro calendar | what is moving right now |
| **RESEARCH** | watchlist, chart, fundamentals, news, screener | studying one name |
| **TRADE** | watchlist, chart, playbook, alerts | entries, stops, targets |
| **PORTFOLIO** | holdings, sector exposure, sector map | what you hold |
| **ANALYST** | chat, news, watchlist | asking questions |

Track sizes are saved **per tab** — a layout dragged for RESEARCH does not follow
you to TRADE, where different panels occupy that space.

### Building your own tab

`+` opens a builder: name it, pick a template, pick panels.

Nineteen presets — FOCUS + RAIL, QUARTERS, THREE COLUMNS, MAIN + 2 RAIL,
TOP + BOTTOM, RAIL + MAIN + RAIL, SINGLE, MAIN + 3 RAIL, TOP + 2 BELOW,
2 ABOVE + BASE, SIX (3x2), HALF + 2, L-SHAPE, HEADER + 3, 3 + FOOTER,
CENTRE STAGE, EIGHT (4x2), NINE (3x3), FOUR ROWS — each shown as a miniature of
the actual arrangement, because "MAIN + 2 RAIL" means nothing until you have
used it once.

Plus a **custom grid**: pick columns and rows from 1, 2, 3, 4 or 6 for any
tiling up to 6x6. Those are the divisors of the 24-unit lattice, so every
generated slot lands on whole units with nothing left over — which is why the
lattice is 24 rather than 20 or 25. All 25 combinations are asserted to tile
completely by running the real generator.

The panel list names everything available in plain words: Chart, Watchlist,
Fundamentals, News, Screener, Scanner, Alerts + Playbook, Macro Calendar,
Analyst. Panels fill the template's slots in the order you click them, and the
builder says how many slots are left. A custom tab drags, resizes and saves its
arrangement exactly like a built-in one; × deletes it.

Every template tiles its canvas completely — asserted cell by cell — so a
custom tab cannot ship with the dead space the built-ins avoid.

Anything opened from the command line appears as a sixth, closable tab over the
current workspace; × or `Esc` returns you to it.

`MAIN` also lists the Bloomberg functions this terminal **deliberately does not
implement** — EMSX, PEOP, the fixed-income suite, CRPR and the rest — each with
the reason. They need a licensed data feed, a brokerage connection or a private
network. A code that opens an empty screen is worse than one that does not
exist, but silently omitting the best-known functions would leave you wondering
whether you typed it wrong.

Any panel maximises to fill the grid (⤢, or `x` over it), and the whole app goes
fullscreen with `f`.

| Key | |
|---|---|
| `1` `2` `3` | Switch workspace |
| `/` | Search symbols |
| `a` | New price alert |
| `c` | Ask the analyst |
| `f` | Fullscreen |
| `x` | Maximise the panel under the cursor |
| `Esc` | Close / un-maximise |
| `?` | Shortcut list |

</details>

<details>
<summary><b>Configuration</b></summary>

Intervals and paths live in `backend/config.py`.

</details>

<details>
<summary><b>AI (optional)</b></summary>

Two features use a model: one-line news summaries with a sentiment tag, and the
analyst chat. **Both are optional** — without credentials the app runs normally
and says which key is missing.

Gemini is preferred when its key is present, because enrichment fires on every
new article and chat spends one request per lookup; Anthropic bills per call.
Set `chat_provider` in `backend/config.py` to `"anthropic"` or `"gemini"` to
force one.

Each article is summarised exactly once and cached forever, so cost scales with
new headlines, not with page refreshes.

**On Gemini's free tier**, the binding limit is requests per *day*, per model
(20/day for some models). A tool-using chat answer can spend a dozen. The agent
therefore falls back down a chain of models — each has its own daily allowance —
and waits out per-minute limits rather than failing. `gemini-flash-lite-latest`
is the default because it measured 12/12 successful requests where
`gemini-flash-latest` managed 1/12.

</details>

<details>
<summary><b>Alerts and the honest bit about grades</b></summary>

The engine scores seven confluence factors across three timeframes and emits a
setup with an entry, stop and two targets — all computed from ATR and swing
structure, never from a model. Setups are graded A+, A or B.

**The grades do not reliably rank.** Replaying the engine over cached history
for the current watchlist:

| Ticker | A+ | A | B |
|---|---|---|---|
| AAPL | 38% | 52% | 43% |
| NVDA | 43% | 24% | 27% |
| MSFT | 42% | 67% | 60% |
| AMZN | 25% | 12% | 25% |

A+ is best on some names and worst on others. Positive expectancy, where it
exists, comes from the 2R:1R target structure — which breaks even at 33% — not
from the letter. Several tickers are outright negative.

For that reason **no grade is ever shown without its measured record**: the
alerts panel, the Discord push and the chat all carry the win rate and average
R behind the letter, or say plainly that it is unvalidated. Thresholds were
deliberately not tuned to make the grades look better.

### Do the factors actually work? (`FACT`)

The engine scores seven confluence factors as if they weigh the same. Nobody
had checked, so `FACT` replays `analyse` over every cached daily bar and reports
each factor's record with it present versus absent.

Over **36,180 resolved setups across 18 tickers**:

| Factor | With | Without | Edge |
|---|---|---|---|
| momentum | 42.5% · +0.275R | 39.7% · +0.192R | **+2.8pp** |
| structure | 42.4% · +0.274R | 40.7% · +0.222R | +1.7pp |
| multi_timeframe | 42.3% · +0.269R | 41.4% · +0.243R | +0.9pp * |
| trend | 42.3% · +0.268R | 41.4% · +0.243R | +0.8pp |
| macd_cross | 42.3% · +0.269R | 41.7% · +0.250R | +0.6pp |
| volume | 41.6% · +0.248R | 41.8% · +0.254R | **−0.2pp** |
| not_extended | 41.6% · +0.247R | 42.6% · +0.278R | **−1.0pp** |

And confluence does not stack the way the grades imply:

```
score 3/7  n= 8,286   42.3%  +0.268R
score 4/7  n= 4,789   43.8%  +0.315R   <- best
score 5/7  n= 8,426   41.7%  +0.252R
score 7/7  n=   172   41.9%  +0.256R
```

Two things follow. `not_extended` measures **worse than useless** — extended
setups did better. And **4 of 7 is the peak**, not 7 of 7, which is precisely
why A+ does not beat B.

\* The harness passes one daily frame for 1h, 4h and 1d, so `multi_timeframe`
compares a frame against itself and collapses to `trend AND momentum`. Read
that row as an artefact of the measurement, not evidence about multi-timeframe
confirmation.

**None of this has been fed back into the engine.** Dropping the factors that
measure badly would be fitting thresholds to this sample — 18 correlated
large-cap names over two years — which is the mistake the backtest exists to
catch. It is a question to investigate, not a verdict to act on.

</details>

<details>
<summary><b>The playbook</b></summary>

`/api/signals` only lists setups that actually fired, which on a normal day is
none of them — and "no signals" says nothing about what you hold. The
**Playbook** panel therefore shows entry, stop, T1 and T2 for *every*
watchlist ticker, computed from the same ATR and swing arithmetic either way,
with an explicit verdict.

```
TSLA  LONG  TRADEABLE  A · 2.00R · 5/7 confluence
      ENTRY 355.49  STOP 350.00 (-1.54%)  TP1 366.47 (+3.09% · 2.0R)  TP2 371.96 (+4.63% · 3.0R)
      macd_cross, momentum, not_extended, structure, trend · 1h,4h,1d
      24% win rate · -0.29R average · measured over 71 past grade-A signals

AAPL  LONG  NO SETUP   2.00R · 3/7 confluence
      ENTRY 313.49   STOP 310.64   T1 319.19   T2 322.03
      Only 3 of 7 confluences — missing macd_cross, multi_timeframe, structure,
      trend. Below the 4-factor minimum, so these levels are what a trade would
      look like, not a reason to take one.
```

Each level carries the move it represents: a stop is not just a price but a
percentage of your capital, and "TP1 366.47" does not say what you would make
until it says "+3.09%". The R multiple beside it compares that to what is
actually at risk.

The win rate never appears without its sample size. The scanner backtests the
names it grades — they are rarely on the watchlist, so nothing has measured
them in advance — and the results are often sobering: a grade-A pick showing
0% over five past signals is a real output, not a bug.

Weak rows are dimmed and say what is missing rather than being hidden — a
ticker vanishing from the list is indistinguishable from a ticker nobody
checked. Verdicts are `TRADEABLE`, `NO SETUP`, `EARNINGS BLACKOUT` and
`NO DATA YET`, and actionable ones sort first.

`risk_reward` is always 2.00: T1 is defined as entry + 2 × risk. It is a
property of the plan's shape, not a filter that some setups pass.

Set `DISCORD_WEBHOOK_URL` to have setups pushed to a channel. The webhook URL is
itself a credential — anyone holding it can post — so it is never logged.

</details>

<details>
<summary><b>World yields (`WB`)</b></summary>

The US Treasury constant-maturity curve from FRED, short end first, with a bar
per maturity — the shape is the only reason to open this screen and a column of
numbers hides it. The change column compares with roughly a month earlier.

Both quoted spreads are computed rather than left for you to subtract: 10Y−2Y,
which is the one people quote, and 10Y−3M, which has the better research record
and is what the New York Fed builds its recession probability from. An inverted
spread says so in words.

A maturity that fails or has no recent print is dropped rather than taking the
curve with it, and FRED's `.` placeholder for a market closure is skipped rather
than parsed — read as a number it raises, and treated as zero it draws a cliff.

Only US Treasuries, despite the name. `WB` is the Bloomberg code for this
screen; this terminal has one free macro source and it is American.

</details>

<details>
<summary><b>Stale bars are refused, not served</b></summary>

The engine only ever asked whether it had *enough* bars. That is a different
question from whether they are *recent*, and the second one matters more: too
few bars produces no plan, while old bars produce a plan that looks exactly
like a good one — entry, stop, targets, a grade and a measured win rate.

The poller only keeps the selected horizon's frames warm, and switching horizon
schedules the fetch in the background so the response stays fast. So switching
to `scalp` returned a full playbook computed from whatever was last cached.
Measured on a live watchlist, that was a session behind on every ticker and
eight days behind on one:

| | cached 5m close | live quote | apart |
|---|---|---|---|
| TSLA | 356.99 | 382.12 | **+7.0%** |
| META | 576.11 | 610.68 | **+6.0%** |
| NVDA | 224.40 | 229.99 | +2.5% |

A scalp plan on those is not slightly wrong. It is a plan for a different
market, and it arrives looking as confident as any other.

An intraday frame behind the last session is now refused, and the card says so
instead of showing levels. Daily and slower are exempt — a daily bar being a
day old is what a daily bar is.

The test is not "younger than N hours". Markets close, so on a Monday morning
the newest 5-minute bar is legitimately three days old and a clock rule either
rejects that or accepts a genuinely stale Wednesday. Instead each intraday
frame is compared against the same ticker's newest **daily** bar, which is the
cheapest available answer to "when did the market last trade" and needs no
market calendar, holiday table or timezone reasoning.

The forward test applies the same guard before arming: a plan judged against
bars it was not really computed from is worse than one not recorded at all.

</details>

<details>
<summary><b>The forward test (`FWD`)</b></summary>

Everything else this terminal claims about the engine comes from a backtest,
and every backtest is in-sample. `FACT` already found the tell: 4-of-7
confluence outperforms 7-of-7, which is what overfitting looks like from the
inside. No further backtesting settles that.

So the engine now keeps its own record going forward. When it calls a setup
tradeable, the plan is written down exactly as computed and left alone. Bars
that arrive afterwards decide whether it filled and whether it hit the stop or
the target. Nothing is re-derived and nothing is tuned. It runs on the poller,
because a forward test you have to remember to start only records the days you
were watching.

It will take months to say anything. That is the nature of the thing.

Two rules make it honest rather than flattering:

- **The stop is checked first within each bar.** With only OHLC there is no way
  to know which came first intrabar, and assuming the target is exactly how a
  backtest flatters itself.
- **A gap through the stop exits at the open, not at the stop.** This is the
  single most common way a paper record overstates itself. Replaying the cached
  history, **12.5% of trades lost more than 1R** this way — losses the factor
  study, which assumed every stop held at exactly −1R, could not see.

The screen also reports the **fill rate**. 8% of plans never traded at their
entry in that replay, and a strategy whose entries rarely fill is not the
strategy the backtest measured.

Plans whose stop sits within 0.25% of the entry are refused rather than
recorded. A stop inside the spread cannot be held by a real order, and dividing
by it manufactures enormous R from an ordinary move — one such plan produced
−28R and dragged a 5,000-trade sample's mean to −202R before this guard
existed.

`FWD` prints where the model is kind — assumed fills, no commission, no
slippage — beside the numbers rather than behind them.

</details>

<details>
<summary><b>Positions and the record</b></summary>

The watchlist is a list of things to look at. Positions are the things actually
held, and the difference is what lets the terminal answer "how am I doing" with
a measurement instead of a feeling.

**Sizing.** Set an account size and a risk percentage in Settings, and every
playbook card grows a share count: how many shares put exactly that percentage
at risk between the entry and the stop. It is arithmetic on two numbers already
on the screen — no broker, no order, no view. Shares are always **floored,
never rounded**: rounding 4.7 up to 5 risks 6% more than the number you typed,
which is the quiet overshoot this exists to prevent. If the risk budget asks
for more shares than the account can pay for, it says so and reports the
smaller real risk rather than the one you asked for.

**Open positions** show what each has at risk, and the panel totals the number
that actually decides whether to take one more: *what you lose if every open
stop is hit today*.

**The record** measures closed trades in R — multiples of the risk taken —
because R is the only unit that makes a 40-share trade and a 400-share trade
comparable, and the only one that survives changing account size. It reports:

| | |
|---|---|
| **Expectancy** | Average R per trade. The number that decides whether any of this is worth doing: 35% at +2R beats 60% at +0.4R, and only expectancy says so. |
| **Win rate** | Never without its count. |
| **Past the stop** | Losses worse than −1R — a gap, slippage, or a stop that got moved. It is the failure mode that quietly turns a positive expectancy negative, so it is counted separately rather than averaged in. |
| **Left on table** | Planned R minus realised R on the winners. This is what "I cut my winners early" looks like as a number. |
| **By grade** | Whether *your* A setups beat *your* B setups. The backtest already showed the engine's A+ does not reliably beat its B; whether that holds for the trades you took is a different question on a far thinner sample, so every row states its n. |

Open positions are excluded from all of it. An unrealised gain is not a result,
and counting it would flatter every summary taken during a rally. Under twenty
closed trades the panel says so in as many words — the numbers are shown, since
hiding them invites guessing, but they should not change what you do.

Entry price and share count cannot be edited after the fact, in the API or in
the database layer: they are what every R is measured against. Stops move,
notes grow, and closing is one-way — a second exit price would silently
overwrite the result the record already measured.

The plan is copied onto the position at entry rather than referenced, because
plans are recomputed continuously and judging a trade against a plan the engine
has since revised would measure nothing.

</details>

<details>
<summary><b>Market scanner</b></summary>

Scans the whole US market for liquid movers, then grades the best of them.

Stage 1 filters every US stock **server-side** on your configured screener
(`SCREENER_SCAN_URL`) — price above your floor, ten-day average volume above
your floor — and returns the pre-market gap and the volume behind it. This is
the only practical way to look at every symbol; fetching twelve thousand quotes
from Yahoo is not. With no screener configured this stage returns nothing and
the panel says so.

Stage 2 runs the same signal engine over cached intraday bars for the top eight,
producing the same entry/stop/T1/T2 as the playbook. A name selected for gapping
this morning is not a two-week swing idea, so it is graded on the intraday
horizon rather than daily.

Defaults are price > $15 and 10-day average volume > 10M, both editable in the
panel. Rescans on demand, and every 15 minutes on its own.

**Ranking is arithmetic on measured fields, not a guess.** Every term earns its
weight:

- **Direction, not magnitude.** The engine is long-only — all seven of its
  factors want an uptrend — so ranking by |move| filled the list with big
  decliners that then scored 0/7 and crowded out the names worth grading.
  Measured: an unweighted |move| put a −39% stock at the top. Declines are
  listed, just not promoted; a controlled pullback in an uptrend is a
  legitimate long entry and the engine decides.
- **Gaps are capped at 15%.** Beyond that it is usually a halt, an offering or
  a reverse split.
- **Volume behind the move.** A 5% gap on 200 shares is a quote, not a market,
  and is marked down explicitly.
- **Room to run.** RSI above 80 or below 20 is penalised — entering there puts
  the stop further from entry for the same target.
- **Liquidity, logarithmically.** 100M average volume is better than 10M, but
  not ten times better.

The session decides which field is live: pre-market ranks on the gap, the
regular session on the change and relative volume. `premarket_change` holds
yesterday's gap once the market opens, so it is never displayed then.

Session boundaries are US Eastern — pre-market 04:00, open 09:30, close 16:00,
after-hours to 20:00. Holidays are deliberately not modelled: the scanner reads
live quotes, so a holiday returns an unchanged list rather than wrong data, and
a hand-maintained holiday table is one more thing to go stale.

</details>

<details>
<summary><b>Adding a ticker</b></summary>

A newly added ticker is warmed immediately — quote, sparkline, news (with AI
summaries and link checks), fundamentals, earnings and bars — rather than
waiting for the ordinary cadence, which is fifteen minutes for news and an hour
for fundamentals. Measured: news and fundamentals land about three seconds
after the add, and the add request itself returns instantly because the warm
runs as a background task.

While a warm is in flight the panels say "Fetching…" rather than "No recent
news". The difference matters: one is a claim about the ticker, the other is a
claim about us.

</details>

<details>
<summary><b>The analyst's replies</b></summary>

The model answers in Markdown. It is rendered — headings, tables, lists, bold —
because a wall of literal `###` and `**` is technically safe and practically
unreadable.

The reply is **escaped first and formatted second**, so every tag in the output
is one the renderer wrote; the model's angle brackets stop being angle brackets
before any pattern runs. Links are limited to `http(s)` — a model-supplied
`javascript:` URL is exactly what must not become a live link — and code spans
are lifted out before the bold and italic passes, which otherwise reach inside
them.

</details>

<details>
<summary><b>Symbol catalogue</b></summary>

Clicking the add box opens a symbol browser over the instruments pulled from
your configured screener (`SCREENER_SCAN_URL`). Roswell ships without one, so
this list starts empty apart from the ten curated indices — see
[Bring your own screener](#bring-your-own-screener). The counts below are what
a full US-market screener typically yields.

| Class | Count | yfinance form |
|---|---|---|
| Stocks | 11,833 | `AAPL` |
| Funds & ETFs | 6,601 | `SPY` |
| Forex | 4,039 | `EURUSD=X` |
| ADRs | 1,579 | — |
| Crypto | 928 | `BTC-USD` |
| Indices | 10 | `^GSPC` |

Only classes that map to something yfinance can actually quote are offered.
A full screener also serves tens of thousands of futures (`EUREX:EAIFJ2027`)
and exotic crypto rows (`PANCAKESWAP:BTCUSDT_5840B7.USD`); neither maps to a
priceable symbol, so neither is offered — a category the app cannot price is
worse than no category. Indices are curated in code, because a stock screener
returns nothing for `type=index`; they are therefore the one part of the
catalogue that works with no screener at all.

Forex and indices have no market cap, so they carry an explicit sort rank —
otherwise the FOREX tab opens on `AEDAUD`.

Search runs against a local SQLite copy, so typing costs nothing and works
offline. The browser opens on the whole catalogue, largest first, loading more
as you scroll; category chips filter it and `+` adds without closing, so you can
add several at once. Refresh from Settings (~3s for all 25,000).

The catalogue is for *finding* a ticker, not trusting one. Screeners and
Yahoo disagree about formatting — a screener writes `NYSE:BRK.B`, Yahoo wants
`BRK-B`, and the dot form returns a quote with no market cap rather than
failing — so symbols are converted, and adding still goes through Yahoo
validation.

</details>

<details>
<summary><b>Your own price alerts</b></summary>

Set an alert from the Alerts panel: ticker, above/below, price, optional note.
The poller checks them against the quotes it just fetched, so an alert cannot
lag the price it is watching, and each fires once — a price oscillating around
the threshold would otherwise deliver every cycle. Alerts on tickers you are
not watching never fire, because nothing quotes them.

</details>

<details>
<summary><b>Settings</b></summary>

The Discord webhook can be set from the Settings drawer instead of an
environment variable, and applies immediately without a restart. It is a
credential — anyone holding the URL can post to your channel — so it is stored
locally, shown only masked (`…/PROB••••••••`), and never returned to the
browser or written to a log. Non-Discord URLs are rejected rather than silently
posting your setups to whatever host was pasted.

Clearing it falls back to `DISCORD_WEBHOOK_URL` if that is set, rather than
going dark.

</details>

<details>
<summary><b>Price projections</b></summary>

The analyst can answer "where is this going" using a projection computed from
measured volatility, not a guess: a lognormal band at 68% and 95% over roughly
one week, one month and one quarter, plus the probability of exceeding a
specific price. Trend extrapolation is damped, because a stock that ran 40% in a
quarter did not thereby earn a 160% annual forecast.

It is a statistical range implied by recent behaviour, not a prediction. It
assumes the future resembles the recent past, which is exactly what earnings and
news break — so the analyst is instructed to pair it with those.

</details>

<details>
<summary><b>News link checking</b></summary>

Cached headlines rot. Every article URL is validated and classified as working,
dead (404/410), paywalled/bot-blocked, or unreachable; dead headlines render
struck through. Transient failures are retried rather than written as verdicts,
so one outage cannot brand a working link broken. Ask the analyst for a summary
of link health at any time.

</details>

<details>
<summary><b>Screener and macro calendar</b></summary>

The screener filters a cached S&P 500 universe. Fundamentals are warmed one
ticker per poll cycle to stay under Yahoo's rate limits, so coverage grows over
the first several hours of uptime — the panel always reports how much of the
universe it has actually seen rather than implying it screened everything.

The macro calendar needs a free FRED key:

```bash
echo 'FRED_API_KEY=your-key-here' >> .env
```

Without one the app runs normally and the calendar explains what's missing.
FRED publishes ~957 releases a month with no impact rating, so
`backend/data/macro_releases.py` curates the 16 that move markets and assigns
each an impact tier. Edit that file to add or re-rank releases.

The universe list is `backend/data/sp500.txt`, one ticker per line — edit it
freely. Note that yfinance wants dashes, not dots (`BRK-B`, not `BRK.B`).


</details>

<details>
<summary><b>Options (`OMON`)</b></summary>

`OMON AAPL` opens the option monitor: a two-sided chain centred on the money,
with the expiry picker across the top.

Four numbers are derived from the chain, and only four, because they are the
ones that come out of quoted prices without an assumption bolted on:

| | |
|---|---|
| **ATM implied volatility** | The at-the-money call and put averaged. One side alone can be stale or quoted wide; parity says the two should agree, so disagreement is a data problem rather than a signal. |
| **Implied move** | The at-the-money straddle — what the market charges for a move in either direction by that expiry. Uses the bid/ask mid, falling back to the last trade when a contract has no two-sided quote. |
| **Put/call ratios** | Volume and open interest, each side totalled separately. With no calls the ratio reports unknown rather than `0.0`, which would read as "no put interest". |
| **Max pain** | The strike where the most open interest expires worthless. It is here because every options screen has it, not because price is shown to gravitate to it — the README will not pretend otherwise and neither does the panel. |

Nothing here prices an option. A theoretical value needs a rate, a dividend
assumption and a volatility view, and a number resting on three guesses would
look far more authoritative than it is. A test asserts the module contains no
Black-Scholes.

The summary needs a spot price, which comes from the cached quote — so a
ticker that is not on your watchlist shows the strike ladder but reports the
derived figures as unknown. Taking the middle of the ladder as a spot would be
a guess dressed up as a measurement.

Chains are fetched on demand and cached for 120 seconds. There is no poller
behind this: quotes move, and a chain from an hour ago is not an answer.


</details>

<details>
<summary><b>Locking the terminal</b></summary>

A password locks the terminal, optionally with Touch ID and optionally with a
second factor. It binds to loopback, which keeps the network out but not
whoever is sitting at the machine — and it holds API keys and a webhook that
can post to a channel.

**Passwords** go through **Argon2id** at the RFC 9106 profile (64 MiB, t=3,
p=4). PBKDF2 at 600k iterations is still OWASP's floor and is not a weak hash,
but it is only CPU-hard, so a GPU runs thousands of guesses in parallel;
Argon2id makes every guess hold 64 MiB, which is what stops racks of GPUs being
the cheap way in. Hashes written before the switch still work and are rewritten
in the new format on the next correct login — no reset, no lockout.

**Two-factor** is TOTP, the same thing Google Authenticator and 1Password
speak. Settings hands you a key to type into the app; nothing is switched on
until a code from it verifies, so a half-finished setup cannot lock you out.
Once on, the password alone returns `totp_required` and no session. Each code
is spent once — without that, a code read over your shoulder stays good for the
rest of its 30 seconds. Ten single-use backup codes are shown once and stored
only as hashes.

**Failed attempts** lock the terminal for 30 seconds, then a minute, then two,
doubling to an hour. The previous flat 60-second lockout reset its counter
afterwards, which allowed eight guesses a minute forever — about eleven
thousand a day.

**Sessions** live in memory only, so a copied database file is not a login.
They expire twelve hours after issue and one hour after they stop being used:
the first bounds a stolen token, the second bounds an unattended desk.

**Requests** are refused unless the `Host` header names a loopback address.
Loopback binding keeps the network out but not a web page — a site you visit
can point its own hostname at 127.0.0.1 and have your browser make the requests
for it. That is DNS rebinding, and the browser sends the attacker's hostname,
so the hostname is the tell.

To reach the terminal by another name — a VPN address, a tunnel — list it in
`ROSWELL_ALLOWED_HOSTS`, comma separated. An entry beginning with a dot matches
any subdomain of it, which is what makes tunnels that hand out a fresh random
name on every run usable:

```bash
ROSWELL_ALLOWED_HOSTS=mac.tail1234.ts.net,.trycloudflare.com
```

Exposing the terminal beyond your own machine changes the threat model: the
lock screen becomes reachable by strangers. Turn on the second factor before
you do it, and prefer a tunnel that can require a login of its own.

Responses carry `X-Frame-Options: DENY` (a page that frames the terminal can
float invisible controls over it and collect the clicks), `nosniff`, and
`Referrer-Policy: no-referrer`.

**There is no password reset.** Recovery paths are how most accounts actually
fall, and a local single-user app does not need one. If you forget the password
and have no backup code, delete the `auth_password_hash` row from the
`settings` table — which requires access to the machine and the file, which is
the point.

### Reaching it from another device

The terminal binds to loopback by default, so nothing but this machine can see
it. To reach it over a private network (Tailscale, NetBird, ZeroTier) or a
tunnel, two things have to change together:

```bash
# 1. in .env — the hostname the other device will use
ROSWELL_ALLOWED_HOSTS=your-machine.netbird.cloud,100.96.90.82

# 2. bind beyond loopback
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

`0.0.0.0` is IPv4 only. `::` is *not* a safe substitute on macOS, which
defaults `IPV6_V6ONLY` on and would stop answering on 127.0.0.1 entirely.

Binding to every interface also puts the port on whatever wifi you are joined
to. What keeps that from mattering is the `Host` allowlist above: a request
arriving at the LAN address carries that address in `Host`, which is not on the
list, and is refused. Turn on the second factor before doing any of this — the
lock screen stops being the last line of defence and becomes the first.

**Touch ID will not work over a private network.** WebAuthn requires a secure
context, which means HTTPS or literally `localhost`; a passkey is also bound to
the hostname it was registered on. Use the password, with TOTP.

### What this does not do

Honesty is more useful than a longer list:

- **No HTTPS.** There is no wire to tap: the traffic never leaves the loopback
  interface, and browsers already treat `http://localhost` as a secure context,
  so cookies, WebAuthn and the rest all behave. A self-signed certificate would
  add warnings and no security.
- **No IP blocking.** Every request comes from 127.0.0.1, so an address is not
  a thing to block. The lockout is global instead, which for one user is the
  same control.
- **Nothing stops malware already running as you.** It can read the database,
  the `.env` and the session memory regardless of any of the above. A lock
  screen answers "someone walked up to my laptop", not "my machine is
  compromised" — no login page has ever answered the second.

</details>

<details>
<summary><b>Credentials</b></summary>

Put keys in a `.env` file next to this README — the app loads it at startup, so
there is no need to `export` anything first:

```
GEMINI_API_KEY=...          # free tier; preferred for chat and news summaries
ANTHROPIC_API_KEY=sk-ant-...# optional fallback, bills per call
FRED_API_KEY=...            # optional; macro calendar
DISCORD_WEBHOOK_URL=...     # optional; pushes computed setups
SCREENER_SCAN_URL=...       # optional; symbol catalogue, Scanner and IMAP
```

`.env` is gitignored and must never be committed. Copy `.env.example` to start.

An already-exported variable always wins over the file, so a shell override is
never clobbered. Every key is optional: without one the corresponding feature
reports itself off and the rest of the app works unchanged.

</details>

<details>
<summary><b>Packaging a desktop app</b></summary>

The same PyInstaller spec builds on all three platforms — `BUNDLE` is only
honoured on macOS and ignored elsewhere, so the one file produces an `.app`, a
`.exe` folder, or a Linux binary as appropriate.

```bash
uv pip install pywebview pyinstaller
uv run pyinstaller roswell.spec --noconfirm
```

| Platform | Output | Launch |
|---|---|---|
| macOS | `dist/Roswell.app` (~101 MB) | `open "dist/Roswell.app"` |
| Windows | `dist/Roswell/Roswell.exe` | double-click, or run it from a shell |
| Linux | `dist/Roswell/Roswell` | `./dist/Roswell/Roswell` |

**User data lives outside the bundle** — deliberately, because a packaged app
may be read-only once signed, and reinstalling would otherwise destroy the
watchlist and cache:

| Platform | Database and `.env` |
|---|---|
| macOS | `~/Library/Application Support/Roswell/` |
| Windows | `%APPDATA%\Roswell\` |
| Linux | `~/.local/share/roswell/` |

A packaged build also looks for `.env` beside the executable itself, which is
where someone who downloaded it would naturally drop the file.

Running from a checkout — the normal case — puts both in the project root
instead, so the development workflow is unchanged.

**macOS only:** the build is unsigned, so the first launch needs
right-click → Open, or `xattr -dr com.apple.quarantine "dist/Roswell.app"`.
Signing requires an Apple Developer account, which this project does not
assume.

</details>

<details>
<summary><b>What the fundamentals panel shows</b></summary>

Beyond the basics it carries PEG and price/book; gross, operating and net
margins; ROE, debt/equity and free cash flow; revenue and earnings growth; the
analyst rating, count and target range; short interest, institutional holding,
average volume and the 50/200-day averages.

Percentages are stored as fractions and rendered as percentages — a 40.3% margin
displayed raw reads as 0.40, which is the kind of thing nobody notices until it
has misled them.

The watchlist sorts by symbol, price or change — click a header.

</details>

---

## License

MIT — see [LICENSE](LICENSE).
