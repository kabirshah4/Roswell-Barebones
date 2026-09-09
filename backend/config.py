"""Tunable settings. No logic belongs in this module."""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Where the CODE and bundled assets live. Inside a PyInstaller bundle this
# resolves into the read-only app payload, which is correct for the frontend
# and the SQL schema.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

IS_FROZEN = bool(getattr(sys, "frozen", False))


def user_data_dir() -> Path:
    """Where the database and .env belong.

    Running from a checkout this is the repo, so the developer workflow is
    unchanged. In a packaged app it must NOT be the bundle: an .app is
    read-only once signed, and anything written inside it is destroyed on
    reinstall -- taking the user's watchlist, cached bars and API keys with it.
    """
    if not IS_FROZEN:
        return PROJECT_ROOT
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Roswell"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "Roswell"
    else:
        base = Path.home() / ".local" / "share" / "roswell"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return PROJECT_ROOT
    return base


def env_search_paths() -> list[Path]:
    """Every place a .env might reasonably sit, in priority order.

    A packaged app has no working directory the user controls, so it also
    looks beside the .app bundle itself -- which is where someone who
    downloaded it would naturally drop the file.
    """
    paths = [user_data_dir() / ".env", PROJECT_ROOT / ".env", Path.cwd() / ".env"]
    if IS_FROZEN:
        # sys.executable is .../Roswell.app/Contents/MacOS/Roswell
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:
            if parent.suffix == ".app":
                paths.append(parent.parent / ".env")
                break
    seen, ordered = set(), []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _load_env_file(path: Path | None = None) -> int:
    """Read .env into the environment. Returns the number of keys set.

    Every credential in this project is read from os.environ, and nothing was
    putting .env there -- the app only ever saw keys because the operator
    happened to `source .env` first. A packaged build has no shell to do that,
    and neither does `uv run uvicorn ...` straight from the README.

    An already-set variable always wins: an explicit export is a deliberate
    override and must not be clobbered by a stale file. Parsing is deliberately
    minimal (no interpolation, no multi-line values) so this stays a config
    file reader and not a shell.
    """
    candidates = [path] if path is not None else env_search_paths()

    text = None
    for candidate in candidates:
        try:
            text = candidate.read_text()
            break
        except (OSError, UnicodeDecodeError):
            continue
    if text is None:
        return 0

    loaded = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


_load_env_file()


@dataclass(frozen=True)
class Config:
    db_path: Path = field(default_factory=lambda: user_data_dir() / "terminal.db")
    frontend_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "frontend")
    quote_interval_seconds: float = 45.0
    sparkline_every_n_cycles: int = 7
    backoff_ceiling_seconds: float = 300.0
    news_every_n_cycles: int = 20
    fundamentals_every_n_cycles: int = 80
    max_enrichments_per_cycle: int = 10
    max_link_checks_per_cycle: int = 15
    link_max_attempts: int = 3
    max_alerts_per_cycle: int = 5
    ai_model: str = "claude-opus-5"
    chat_provider: str = "auto"   # auto | anthropic | gemini
    # Chat headroom. A tool-use turn is one provider call, and the tools are all
    # SQLite reads, so a generous ceiling costs latency rather than money or
    # market-data traffic. Answering "what should I buy" properly means walking
    # signals -> backtest stats -> earnings -> news for several tickers, which
    # is a dozen calls before the model has said anything.
    chat_max_turns: int = 24
    chat_max_tokens: int = 8192
    chat_history_turns: int = 60
    # Gemini's free tier limits requests per minute and a tool-use answer costs
    # one request per lookup, so an ordinary question can hit the cap mid-answer.
    # This is the total time one answer may spend waiting out those limits.
    chat_retry_budget_seconds: float = 90.0
    gemini_model: str = "gemini-flash-lite-latest"
    signal_every_n_cycles: int = 40
    earnings_every_n_cycles: int = 320
    bars_1h_period: str = "730d"
    bars_1d_period: str = "2y"
    earnings_blackout_days: int = 3
    macro_every_n_cycles: int = 240
    # ~15 minutes at a 45s cycle, matching how often a pre-market
    # list is worth re-reading.
    # The forward test runs on its own cadence. It only reads cached bars and
    # runs a pure function, so it is cheap; the limit on how fast it can learn
    # anything is how fast new bars arrive, not how often this fires.
    paper_every_n_cycles: int = 4
    scanner_every_n_cycles: int = 20
    universe_max_attempts: int = 3
    # ~5 minutes at a 45s cycle. Yahoo's block outlasts a couple of
    # retries, so a short pause just re-triggers it.
    universe_cooldown_cycles: int = 7
    host: str = "127.0.0.1"
    port: int = 8000


config = Config()
