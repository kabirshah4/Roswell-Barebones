import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Config, config as default_config
from backend.db import database
from backend.routes import auth as auth_routes
from backend.routes import backtest as backtest_routes
from backend.routes import chat as chat_routes
from backend.routes import factors as factors_routes
from backend.routes import functions as functions_routes
from backend.routes import fundamentals as fundamentals_routes
from backend.routes import macro as macro_routes
from backend.routes import news as news_routes
from backend.routes import options as options_routes
from backend.routes import positions as positions_routes
from backend.routes import paper as paper_routes
from backend.routes import plans as plans_routes
from backend.routes import prices as prices_routes
from backend.routes import alerts as alerts_routes
from backend.routes import scanner as scanner_routes
from backend.routes import screener as screener_routes
from backend.routes import settings as settings_routes
from backend.routes import symbols as symbols_routes
from backend.routes import signals as signals_routes
from backend.routes import watchlist as watchlist_routes
from backend.services import auth as auth_service
from backend.services.chat_agent import ChatAgent
from backend.services.gemini_agent import GeminiAgent
from backend.services.claude_client import ClaudeClient
from backend.services.discord_client import DiscordClient
from backend.services.fred_client import FredClient
from backend.services.gemini_enricher import GeminiEnricher
from backend.services.link_checker import LinkChecker
from backend.services.premarket_scanner import PremarketScanner
from backend.services.price_poller import PricePoller
from backend.services.screener_client import ScreenerClient
from backend.services.yfinance_client import YFinanceClient

logging.basicConfig(level=logging.INFO)


def _load_universe() -> list[str]:
    path = Path(__file__).parent / "data" / "sp500.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def _asset_version(path: Path) -> str:
    """Short content hash, or a timestamp if the file cannot be read."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        import time

        return str(int(time.time()))


def _render_index(frontend_dir: Path) -> str:
    """Stamp every referenced asset with a content hash.

    Discovered from the HTML rather than listed here. A hardcoded list means a
    new file gets served from the browser's heuristic cache and the fix sits in
    a file the page never fetches, which cost an afternoon once already.
    """
    html = (frontend_dir / "index.html").read_text()
    for asset in sorted(set(re.findall(r"/static/([A-Za-z0-9_.-]+)", html))):
        path = frontend_dir / asset
        if not path.is_file():
            continue
        html = html.replace(
            f"/static/{asset}", f"/static/{asset}?v={_asset_version(path)}"
        )
    return html


def _symbol_count(db_path: Path) -> int:
    with database.get_conn(db_path) as conn:
        return database.symbol_count(conn)


def _coverage(db_path: Path) -> dict:
    with database.get_conn(db_path) as conn:
        return database.universe_coverage(conn)



def _pick_chat_agent(cfg: Config) -> Any:
    """Choose a chat backend.

    "auto" prefers Gemini when its key is present, because it has a free tier -
    an Anthropic key bills per call. An explicit setting always wins, and a
    provider whose key is missing reports itself disabled rather than failing
    at request time.
    """
    provider = (cfg.chat_provider or "auto").strip().lower()

    def gemini() -> GeminiAgent:
        return GeminiAgent(
            model=cfg.gemini_model,
            max_turns=cfg.chat_max_turns,
            max_tokens=cfg.chat_max_tokens,
            retry_budget_seconds=cfg.chat_retry_budget_seconds,
        )

    def anthropic() -> ChatAgent:
        return ChatAgent(
            model=cfg.ai_model,
            max_turns=cfg.chat_max_turns,
            max_tokens=cfg.chat_max_tokens,
        )

    if provider == "gemini":
        return gemini()
    if provider == "anthropic":
        return anthropic()

    picked = gemini()
    return picked if picked.enabled else anthropic()

def _pick_ai_client(cfg: Config) -> Any:
    """Choose a news-enrichment backend, on the same rule as the chat agent.

    Enrichment fires on every new article across the whole watchlist, so it is
    the app's largest source of AI spend. "auto" therefore prefers Gemini's
    free tier and only falls back to Anthropic, which bills per call.
    """
    provider = (cfg.chat_provider or "auto").strip().lower()

    if provider == "gemini":
        return GeminiEnricher(model=cfg.gemini_model)
    if provider == "anthropic":
        return ClaudeClient(model=cfg.ai_model)

    gemini = GeminiEnricher(model=cfg.gemini_model)
    if gemini.enabled:
        return gemini
    return ClaudeClient(model=cfg.ai_model)


def create_app(
    cfg: Config | None = None,
    client: Any | None = None,
    ai_client: Any | None = None,
    fred_client: Any | None = None,
    chat_agent: Any | None = None,
    link_checker: Any | None = None,
    discord_client: Any | None = None,
    screener_client: Any | None = None,
    scanner: Any | None = None,
    start_poller: bool = True,
) -> FastAPI:
    cfg = cfg or default_config
    client = client or YFinanceClient()
    ai_client = ai_client if ai_client is not None else _pick_ai_client(cfg)
    fred_client = fred_client if fred_client is not None else FredClient()
    chat_agent = chat_agent if chat_agent is not None else _pick_chat_agent(cfg)
    link_checker = link_checker if link_checker is not None else LinkChecker()
    discord_client = (
        discord_client if discord_client is not None else DiscordClient()
    )
    screener_client = (
        screener_client if screener_client is not None else ScreenerClient()
    )
    scanner = scanner if scanner is not None else PremarketScanner()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init_db(cfg.db_path)
        # A webhook saved from the UI outlives the process; apply it before the
        # poller starts so alerts are not silently dropped on the first cycles.
        with database.get_conn(cfg.db_path) as conn:
            stored_webhook = database.get_setting(conn, "discord_webhook_url")
        if stored_webhook and hasattr(app.state.discord_client, "set_webhook_url"):
            app.state.discord_client.set_webhook_url(stored_webhook)
        # A key added since the last run re-queues everything skipped without one.
        if getattr(app.state.ai_client, "enabled", False):
            with database.get_conn(cfg.db_path) as conn:
                requeued = database.reset_skipped_to_pending(conn)
            if requeued:
                logging.getLogger(__name__).info(
                    "Re-queued %d articles for enrichment", requeued
                )
        # Gated like the poller task itself: without a running poller nothing
        # will ever warm these entries, so seeding would only bloat coverage
        # counts for callers (tests, one-off scripts) that opt out of polling.
        if start_poller:
            universe = _load_universe()
            if universe:
                with database.get_conn(cfg.db_path) as conn:
                    added = database.seed_universe(conn, universe)
                if added:
                    logging.getLogger(__name__).info(
                        "Seeded %d universe tickers", added
                    )
        app.state.poller_task = (
            asyncio.create_task(app.state.poller.run()) if start_poller else None
        )
        try:
            yield
        finally:
            task = app.state.poller_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Roswell", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.client = client
    app.state.ai_client = ai_client
    app.state.fred_client = fred_client
    app.state.chat_agent = chat_agent
    app.state.discord_client = discord_client
    app.state.screener_client = screener_client
    app.state.scanner = scanner
    app.state.sessions = auth_service.SessionStore()
    # Half-authenticated logins: the password passed, the second factor has
    # not. Short-lived, and not a session — it opens nothing on its own.
    app.state.pending = auth_service.SessionStore(ttl=120, idle_timeout=120)
    app.state.attempt_limiter = auth_service.AttemptLimiter()
    app.state.poller = PricePoller(
        client, cfg.db_path, cfg, ai_client=ai_client, fred_client=fred_client,
        link_checker=link_checker, discord_client=discord_client,
        scanner=scanner,
    )
    app.state.poller_task = None

    @app.get("/api/health")
    async def health() -> dict:
        poller = app.state.poller
        return {
            "status": "ok",
            "last_success_at": poller.last_success_at,
            "consecutive_failures": poller.consecutive_failures,
            "interval_seconds": poller.current_interval,
            "cycles": poller.cycle_count,
            "ai_enabled": bool(getattr(app.state.ai_client, "enabled", False)),
            "ai_provider": "gemini"
            if type(app.state.ai_client).__name__.startswith("Gemini")
            else "anthropic",
            "fred_enabled": bool(getattr(app.state.fred_client, "enabled", False)),
            "alerts_enabled": bool(
                getattr(app.state.discord_client, "enabled", False)
            ),
            "signals_found": poller.signals_found,
            "price_alerts_fired": poller.price_alerts_fired,
            "symbols_cached": _symbol_count(cfg.db_path),
            "warming": sorted(getattr(poller, "warming", set()) or []),
            "alerts_sent": poller.alerts_sent,
            "chat_enabled": bool(getattr(app.state.chat_agent, "enabled", False)),
            "chat_provider": type(app.state.chat_agent).__name__
            .replace("Agent", "").lower(),
            "universe_coverage": _coverage(cfg.db_path),
        }

    app.include_router(prices_routes.router)
    app.include_router(watchlist_routes.router)
    app.include_router(news_routes.router)
    app.include_router(fundamentals_routes.router)
    app.include_router(screener_routes.router)
    app.include_router(signals_routes.router)
    app.include_router(backtest_routes.router)
    app.include_router(plans_routes.router)
    app.include_router(scanner_routes.router)
    app.include_router(functions_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(factors_routes.router)
    app.include_router(options_routes.router)
    app.include_router(positions_routes.router)
    app.include_router(paper_routes.router)
    app.include_router(symbols_routes.router)
    app.include_router(alerts_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(macro_routes.router)
    app.include_router(chat_routes.router)

    # Registered last so it cannot shadow the /api routes above.
    app.mount(
        "/static", StaticFiles(directory=cfg.frontend_dir), name="static"
    )

    # Paths reachable without a session. Everything else is gated: the terminal
    # holds API keys and a webhook that can post to a channel, and loopback
    # stops the network reaching it but not whoever is sitting at the machine.
    #
    # /docs and /openapi.json used to be here. They publish every route and
    # every request shape to anyone who can reach the port, which is a free map
    # for whoever is probing — and the owner can always read them after logging
    # in.
    OPEN_PREFIXES = ("/api/auth/", "/static/", "/favicon.ico")

    # Loopback binding keeps the network out. It does not keep a web page out:
    # a site you visit can point its own hostname at 127.0.0.1 and have your
    # browser make the requests for it — DNS rebinding. The browser sends that
    # site's name in the Host header, so refusing unfamiliar Host values is the
    # defence, and it is the one that matters most for an app like this.
    ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0",
                     "testserver"}
    # An entry beginning with a dot matches any subdomain of it — the same
    # convention Django uses. Without it, a tunnel that hands out a fresh
    # random subdomain on every run (Cloudflare's free quick tunnels do)
    # would mean editing this and restarting each time, which is how people
    # end up turning the check off entirely.
    ALLOWED_SUFFIXES = {".localhost"}
    for entry in os.environ.get("ROSWELL_ALLOWED_HOSTS", "").split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        (ALLOWED_SUFFIXES if entry.startswith(".") else ALLOWED_HOSTS).add(entry)

    def _host_allowed(request) -> bool:
        host = (request.headers.get("host") or "").split(",")[0].strip().lower()
        if not host:
            return True          # HTTP/1.0 and some probes send none
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1]
        if name in ALLOWED_HOSTS:
            return True
        return any(name.endswith(suffix) for suffix in ALLOWED_SUFFIXES)

    @app.middleware("http")
    async def require_session(request, call_next):
        path = request.url.path
        if path == "/" or path.startswith(OPEN_PREFIXES):
            return await call_next(request)

        # No password configured yet means first run; the setup screen needs a
        # reachable app to set one from.
        with database.get_conn(cfg.db_path) as conn:
            configured = bool(database.get_setting(conn, "auth_password_hash"))
        if not configured:
            return await call_next(request)

        token = request.cookies.get(auth_routes.SESSION_COOKIE)
        if app.state.sessions.valid(token):
            return await call_next(request)

        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    # Registered after require_session, and that ordering is load-bearing:
    # Starlette wraps each new middleware around the previous one, so the last
    # registered is the first to see a request. A rebinding check that runs
    # after the auth check never fires for an unauthenticated request — it
    # answers 401 instead of refusing the host outright.
    @app.middleware("http")
    async def guard_origin(request, call_next):
        from fastapi.responses import JSONResponse

        if not _host_allowed(request):
            return JSONResponse(
                status_code=421,
                content={"detail": "This terminal only answers on localhost."},
            )
        response = await call_next(request)
        # A page cannot frame the terminal and trick a click through it, cannot
        # sniff a response into a different content type, and does not leak the
        # URL to anything it links out to.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        """Browsers ask for this before parsing any HTML, so the <link> tags
        cannot answer it. Left to 404 it would show a blank tab on the lock
        screen, which is the first thing anyone sees."""
        return FileResponse(
            cfg.frontend_dir / "favicon.ico", media_type="image/x-icon"
        )

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        """Serve the page with content-hashed asset URLs.

        StaticFiles sends an ETag but no Cache-Control, so browsers fall back
        to heuristic freshness and can serve a stale app.js from memory without
        ever revalidating. That is not theoretical: a layout fix shipped and
        did not reach the browser, and the bug looked unfixed.

        Hashing the asset contents into the URL makes a changed file a
        different resource, so the browser cannot serve the old one. The HTML
        itself is marked no-store, since it is the thing that carries the
        hashes.
        """
        return HTMLResponse(
            content=_render_index(cfg.frontend_dir),
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
