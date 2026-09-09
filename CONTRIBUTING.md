# Contributing

Thanks for taking a look. This is a small project with strong opinions about
structure, and the fastest way to get a change merged is to match them.

## Getting set up

```bash
git clone https://github.com/kabirshah4/Roswell-Terminal.git
cd Roswell-Terminal
uv sync
uv run pytest
```

The suite is offline and takes about 50 seconds. If it passes, your
environment is correct.

## The rules that matter

These are enforced by tests, not by review. Breaking one will fail CI.

1. **Read routes never touch the network.** Only
   `backend/services/price_poller.py` performs outbound I/O on a cadence.
   Every read route serves SQLite. The single exception is
   `POST /api/watchlist`, which validates a symbol before storing it. If a
   route needs fresh data, the poller should already have cached it.

2. **`yfinance` is imported in exactly one file.**
   `backend/services/yfinance_client.py`. It is an unofficial library and it
   will break; keeping it in one place means one file to repair.

3. **Only five modules may import `httpx`.** Network access is a privilege,
   not a convenience.

4. **The signal engine does no I/O.** `indicators.py`, `signal_engine.py` and
   `forecast.py` are pure functions over price frames. This is what lets the
   backtest replay the real engine over history instead of a copy of it. Keep
   them pure.

5. **Degrade honestly.** A missing credential must name the variable it wants.
   Never fail silently, and never show a confident number computed from data
   you know is stale.

## Tests

Write one. The suite has 1,244 of them and they are the reason the constraints
above stay true.

```bash
uv run pytest                  # offline, the default
uv run pytest -m live          # opt-in, hits the real Yahoo API
uv run pytest tests/test_signal_engine.py -q
```

Tests that need a screener endpoint get one from the `configured_screener`
fixture in `tests/conftest.py`. Tests asserting the unconfigured behaviour
delete it with `monkeypatch.delenv`.

## Pull requests

- One concern per PR.
- Say what breaks if the change is wrong. If you cannot describe a failure
  mode, the change may not be needed.
- Comments should explain *why*, not *what*. The code already says what.

## Reporting bugs

Open an issue with the version of Python you are on, the command you ran, and
what happened instead. If a panel is empty, `curl /api/health` and include the
output — it reports which subsystems are enabled.
