"""Background refresh loop. The only component that performs network I/O.

Route handlers read SQLite exclusively, so outbound API volume is a function
of this poller's interval and the watchlist size — and nothing else.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import Config
from backend.db import database
from backend.services.claude_client import REFUSED

logger = logging.getLogger(__name__)


class PricePoller:
    def __init__(
        self,
        client: Any,
        db_path: Path,
        cfg: Config,
        ai_client: Any | None = None,
        fred_client: Any | None = None,
        link_checker: Any | None = None,
        discord_client: Any | None = None,
        scanner: Any | None = None,
    ) -> None:
        self.client = client
        self.ai_client = ai_client
        self.fred_client = fred_client
        self.link_checker = link_checker
        self.discord_client = discord_client
        self.scanner = scanner
        self._db_path = db_path
        self._cfg = cfg
        self.current_interval: float = cfg.quote_interval_seconds
        self.consecutive_failures: int = 0
        self.last_success_at: str | None = None
        self.cycle_count: int = 0
        self.total_cycles: int = 0
        self.news_cycles: int = 0
        self.enrichments_done: int = 0
        self.universe_warmed: int = 0
        self.macro_events_seen: int = 0
        self.macro_stale: bool = False
        self.macro_last_success: str | None = None
        self.links_checked: int = 0
        self.links_broken: int = 0
        self.bars_written: int = 0
        self.signals_found: int = 0
        self.alerts_sent: int = 0
        self.price_alerts_fired: int = 0
        self.symbols_cached: int = 0
        # Tickers currently being warmed, so the UI can say "fetching" rather
        # than "no news" -- which reads as "this ticker has none".
        self.warming: set[str] = set()
        self.scans_run: int = 0
        # Cycles to skip before touching the universe again. Yahoo
        # rate-limits hard, and hammering it makes the block last longer.
        self.universe_cooldown: int = 0

    async def run(self) -> None:
        """Poll forever. Cancel the task to stop."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in poll cycle")
            await asyncio.sleep(self.current_interval)

    async def run_once(self) -> bool:
        """Run a single refresh cycle. Returns False only on a total fetch failure.

        Watchlist polling, universe warming, and the macro refresh are
        deliberately independent: the screener and macro calendar must keep
        filling in even before the user has added a single ticker. Only the
        quote/news/fundamentals work below needs a non-empty watchlist.
        """
        with database.get_conn(self._db_path) as conn:
            tickers = database.list_watchlist(conn)

        self.total_cycles += 1

        result = True
        if tickers:
            result = await self._poll_watchlist(tickers)

        # Watchlist-independent work. Driven by total_cycles, not cycle_count:
        # cycle_count counts only watchlist cycles by contract, so keying the
        # macro cadence to it would stall the calendar on an empty watchlist.
        await self._warm_one_universe_ticker()

        if self.total_cycles % self._cfg.macro_every_n_cycles == 0:
            await self._refresh_macro()
            await self._refresh_yields()

        # Watchlist-independent: the scanner looks at the whole market, so it
        # must run even before a single ticker has been added.
        if self.total_cycles % self._cfg.scanner_every_n_cycles == 0:
            await self._refresh_scan()

        if tickers and self.total_cycles % self._cfg.paper_every_n_cycles == 0:
            await self._run_paper()

        return result

    async def _run_paper(self) -> None:
        """Advance the forward test.

        On the poller rather than on demand, and deliberately: a forward test
        you have to remember to run only records the days you were watching,
        which is a survivorship problem dressed as a track record.
        """
        import asyncio

        from backend.services import paper_trader

        with database.get_conn(self._db_path) as conn:
            horizon = database.get_setting(conn, "horizon")
        try:
            result = await asyncio.to_thread(
                paper_trader.run_cycle, self._db_path, horizon
            )
        except Exception:
            logger.exception("paper: cycle failed")
            return
        if result["armed"] or result["resolved"]:
            logger.info("paper: armed %d, resolved %d (%s)",
                     result["armed"], result["resolved"], result["horizon"])

    async def _poll_watchlist(self, tickers: list[str]) -> bool:
        """Quote/sparkline/news/fundamentals work. Requires a non-empty watchlist."""
        self.cycle_count += 1

        try:
            quotes = await asyncio.to_thread(self.client.fetch_quotes, tickers)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "fetch_quotes raised for %d tickers", len(tickers), exc_info=True
            )
            return self._handle_total_failure(tickers)

        if not quotes:
            logger.warning("No quotes returned for %d tickers", len(tickers))
            return self._handle_total_failure(tickers)

        with database.get_conn(self._db_path) as conn:
            for ticker, q in quotes.items():
                database.upsert_price(
                    conn, ticker, q.price, q.prev_close, q.change_pct,
                    q.volume, q.currency,
                )
            missing = [t for t in tickers if t not in quotes]
            if missing:
                database.mark_stale(conn, missing)

        self._record_success()

        if self.cycle_count % self._cfg.sparkline_every_n_cycles == 0:
            await self._refresh_sparklines(tickers)

        if self.cycle_count % self._cfg.news_every_n_cycles == 0:
            self.news_cycles += 1
            await self._refresh_news(tickers)
            await self._enrich_pending()
            await self._check_pending_links()

        if self.cycle_count % self._cfg.fundamentals_every_n_cycles == 0:
            await self._refresh_fundamentals(tickers)

        if self.cycle_count % self._cfg.earnings_every_n_cycles == 0:
            await self._refresh_earnings(tickers)

        if self.cycle_count % self._cfg.signal_every_n_cycles == 0:
            await self._refresh_bars(tickers)
            await self._scan_signals(tickers)

        # Price alerts are checked against the quotes just written, every
        # cycle: an alert that fires 30 minutes late is not an alert.
        self._check_price_alerts(quotes)

        # Delivery runs every cycle, not only on scan cycles: a webhook that
        # was down when a setup fired should not wait 30 minutes for a retry.
        await self._deliver_alerts()
        await self._deliver_price_alerts()

        return True

    async def _refresh_sparklines(self, tickers: list[str]) -> None:
        for ticker in tickers:
            try:
                points = await asyncio.to_thread(self.client.fetch_intraday, ticker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "fetch_intraday raised for %s", ticker, exc_info=True
                )
                continue
            if points:
                with database.get_conn(self._db_path) as conn:
                    database.upsert_sparkline(conn, ticker, points)

    async def _refresh_news(self, tickers: list[str]) -> None:
        for ticker in tickers:
            try:
                articles = await asyncio.to_thread(self.client.fetch_news, ticker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("News fetch failed for %s", ticker, exc_info=True)
                continue
            if not articles:
                continue
            with database.get_conn(self._db_path) as conn:
                for a in articles:
                    database.upsert_news_article(
                        conn, a.id, a.ticker, a.title, a.publisher,
                        a.url, a.published_at, a.summary,
                    )

    async def _enrich_pending(self) -> None:
        """Summarise up to the per-cycle cap. Never fails the poll cycle."""
        with database.get_conn(self._db_path) as conn:
            pending = database.pending_enrichments(
                conn, self._cfg.max_enrichments_per_cycle
            )
        if not pending:
            return

        ai = self.ai_client
        if ai is None or not getattr(ai, "enabled", False):
            # No credentials: park these rather than retrying forever. They are
            # re-queued at startup if a key appears later.
            with database.get_conn(self._db_path) as conn:
                for item in pending:
                    database.mark_enrich_skipped(conn, item["id"])
            return

        for item in pending:
            try:
                result = await asyncio.to_thread(
                    ai.enrich, item["title"] or "", item["summary"] or ""
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Enrichment raised for %s", item["id"], exc_info=True)
                result = None

            with database.get_conn(self._db_path) as conn:
                if result is REFUSED:
                    # Deterministic for the same input -- retrying would just
                    # bill the same refusal again. Fail immediately without
                    # touching the attempt counter.
                    database.mark_enrich_failed(conn, item["id"])
                elif result is None:
                    attempts = database.bump_enrich_attempts(conn, item["id"])
                    if attempts >= 3:
                        database.mark_enrich_failed(conn, item["id"])
                else:
                    database.set_enrichment(
                        conn, item["id"], result.summary, result.sentiment
                    )
                    self.enrichments_done += 1

    async def _refresh_scan(self) -> None:
        """Re-run the market scan and cache it. Never fails the poll cycle."""
        scanner = self.scanner
        if scanner is None:
            return
        import json

        from backend.services import market_session

        session = market_session.current()
        try:
            candidates = await asyncio.to_thread(scanner.scan, session.name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Scan refresh failed", exc_info=True)
            return
        if not candidates:
            return

        from backend.routes.scanner import _plain

        payload = {
            "candidates": [_plain(c) for c in candidates],
            "as_of": datetime.now(timezone.utc).isoformat(),
            "graded": 0,
            "session": {
                "name": session.name, "label": session.label,
                "is_tradeable": session.is_tradeable,
                "ranks_on": session.ranks_on,
            },
        }
        with database.get_conn(self._db_path) as conn:
            database.set_setting(conn, "scanner_results", json.dumps(payload))
        self.scans_run += 1

    async def warm_horizon(self, horizon_key: str) -> None:
        """Cache the bars a newly selected horizon needs, now.

        Switching to INTRADAY otherwise shows "insufficient_data" for up to
        half an hour while the bar cycle catches up, which reads as the horizon
        being broken.
        """
        from backend.services import horizons as horizon_defs

        spec = horizon_defs.get(horizon_key)
        with database.get_conn(self._db_path) as conn:
            tickers = database.list_watchlist(conn)
        if not tickers:
            return

        for ticker in tickers:
            for interval, period in spec.fetch.items():
                try:
                    rows = await asyncio.to_thread(
                        self.client.fetch_bars, ticker, period, interval
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Horizon warm failed for %s %s", ticker, interval,
                        exc_info=True,
                    )
                    continue
                if rows:
                    with database.get_conn(self._db_path) as conn:
                        self.bars_written += database.upsert_bars(
                            conn, ticker, interval, rows
                        )
        logger.info("Warmed horizon %s", spec.key)

    async def warm_ticker(self, ticker: str) -> None:
        """Fetch everything for one newly added ticker, now.

        Without this a new ticker shows an empty news panel and no
        fundamentals until the next cadence fires -- up to fifteen minutes for
        news and an hour for fundamentals. That reads as "this ticker has no
        data" rather than "we have not looked yet".

        Runs as a background task so the add request stays fast, and never
        raises: a warm that fails just leaves the normal cadence to catch up.
        """
        ticker = ticker.strip().upper()
        self.warming.add(ticker)
        try:
            # Quote first: it is the fastest and the most visible.
            try:
                quotes = await asyncio.to_thread(self.client.fetch_quotes, [ticker])
                with database.get_conn(self._db_path) as conn:
                    for symbol, q in (quotes or {}).items():
                        database.upsert_price(
                            conn, symbol, q.price, q.prev_close, q.change_pct,
                            q.volume, q.currency,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Warm quote failed for %s", ticker, exc_info=True)

            # Each step is independent: one failure must not skip the rest.
            for step in (
                self._refresh_sparklines, self._refresh_news,
                self._refresh_fundamentals, self._refresh_earnings,
                self._refresh_bars,
            ):
                try:
                    await step([ticker])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Warm step %s failed for %s", step.__name__, ticker,
                        exc_info=True,
                    )

            # Summaries and link checks for the news just fetched.
            for step in (self._enrich_pending, self._check_pending_links):
                try:
                    await step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Warm enrichment failed for %s", ticker, exc_info=True)

            logger.info("Warmed %s", ticker)
        finally:
            self.warming.discard(ticker)

    async def _refresh_bars(self, tickers: list[str]) -> None:
        """Cache the OHLCV history the signal engine and forecasts run on.

        Both the engine and the forecast are pure functions over these bars,
        so this is the only place the history enters the system.
        """
        wanted = tuple(self._bar_intervals().items())
        for ticker in tickers:
            for interval, period in wanted:
                try:
                    rows = await asyncio.to_thread(
                        self.client.fetch_bars, ticker, period, interval
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Bar fetch raised for %s %s", ticker, interval, exc_info=True
                    )
                    continue
                if not rows:
                    continue
                with database.get_conn(self._db_path) as conn:
                    self.bars_written += database.upsert_bars(
                        conn, ticker, interval, rows
                    )

    def _bar_intervals(self) -> dict[str, str]:
        """Intervals to cache: the swing set plus the selected horizon's.

        The swing set is always fetched because the signal engine and the
        backtest run on it regardless of what the interface is displaying --
        switching to SCALP must not silently stop the alerts working.
        """
        from backend.services import horizons as horizon_defs

        wanted = dict(horizon_defs.HORIZONS["swing"].fetch)
        try:
            with database.get_conn(self._db_path) as conn:
                selected = database.get_setting(conn, "horizon")
        except Exception:
            selected = None
        if selected and selected != "swing":
            wanted.update(horizon_defs.get(selected).fetch)
        return wanted

    async def _refresh_earnings(self, tickers: list[str]) -> None:
        """Cache earnings dates. A setup near earnings carries gap risk."""
        for ticker in tickers:
            try:
                dates = await asyncio.to_thread(
                    self.client.fetch_earnings_dates, ticker
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Earnings fetch raised for %s", ticker, exc_info=True)
                continue
            if not dates:
                continue
            with database.get_conn(self._db_path) as conn:
                for d in dates:
                    database.upsert_earnings(
                        conn, ticker, d["earnings_at"], d.get("eps_estimate")
                    )

    async def _scan_signals(self, tickers: list[str]) -> None:
        """Run the signal engine over cached bars and persist new setups.

        The engine is pure and does no I/O, so this reads bars out of SQLite,
        hands it frames, and writes back whatever it returns. Nothing here
        touches the network.
        """
        from datetime import datetime as _dt

        from backend.services import signal_engine

        now_iso = _dt.now(timezone.utc).isoformat()
        for ticker in tickers:
            with database.get_conn(self._db_path) as conn:
                frames = _frames_for(conn, ticker)
                earnings_at = database.next_earnings(conn, ticker, now_iso)
            if frames is None:
                continue
            try:
                setup = signal_engine.evaluate(
                    ticker, frames, earnings_at=earnings_at, now_iso=now_iso,
                    blackout_days=self._cfg.earnings_blackout_days,
                )
            except Exception:
                logger.warning("Signal evaluation failed for %s", ticker, exc_info=True)
                continue
            if setup is None:
                continue
            with database.get_conn(self._db_path) as conn:
                if database.upsert_signal(conn, setup, _fingerprint(setup)):
                    self.signals_found += 1

        # Refresh the measured record alongside the scan. A grade is a claim
        # about expected performance, and measurement showed A+ did not
        # reliably outperform B -- so nothing in this app should display a
        # grade without the numbers behind it.
        await self._refresh_backtests(tickers)

    async def _refresh_backtests(self, tickers: list[str]) -> None:
        """Replay the engine over cached bars and cache the per-grade results.

        Cached rather than computed on read: a replay costs ~0.4s per ticker,
        which a panel rendering 25 signals cannot pay.
        """
        from backend.services import backtest

        for ticker in tickers:
            try:
                stats = await asyncio.to_thread(
                    self._run_backtest, ticker, backtest
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Backtest failed for %s", ticker, exc_info=True)
                continue
            if not stats:
                continue
            with database.get_conn(self._db_path) as conn:
                database.upsert_backtest_stats(conn, ticker, stats)

    def _run_backtest(self, ticker: str, backtest: Any) -> dict:
        import pandas as pd

        with database.get_conn(self._db_path) as conn:
            rows = database.get_bars(conn, ticker, "1d", limit=2000)
        if len(rows) < 250:
            return {}
        frame = pd.DataFrame(rows)
        frame.index = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
        frame = frame.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"})
        return backtest.run_backtest(ticker, frame)

    def _check_price_alerts(self, quotes: dict) -> None:
        """Fire any user alert whose condition the latest quote satisfies.

        Evaluated against the quotes this cycle just fetched rather than
        re-reading the database, so the check cannot lag the price it is
        watching. Firing is one-way: an alert triggers once and stays
        triggered, because a price oscillating around the threshold would
        otherwise deliver on every cycle.
        """
        if not quotes:
            return
        with database.get_conn(self._db_path) as conn:
            armed = database.armed_price_alerts(conn)
            for alert in armed:
                quote = quotes.get(alert["ticker"])
                price = getattr(quote, "price", None) if quote else None
                if price is None:
                    continue
                hit = (
                    price >= alert["price"] if alert["direction"] == "above"
                    else price <= alert["price"]
                )
                if hit:
                    database.mark_alert_triggered(conn, alert["id"])
                    self.price_alerts_fired += 1
                    logger.info(
                        "Price alert %d fired: %s %s %s",
                        alert["id"], alert["ticker"], alert["direction"],
                        alert["price"],
                    )

    async def _deliver_price_alerts(self) -> None:
        """Push triggered user alerts to Discord. Never fails the cycle."""
        discord = self.discord_client
        if discord is None or not getattr(discord, "enabled", False):
            return
        with database.get_conn(self._db_path) as conn:
            pending = database.undelivered_price_alerts(
                conn, self._cfg.max_alerts_per_cycle
            )
        for alert in pending:
            above = alert["direction"] == "above"
            title = f"{alert['ticker']} {alert['direction']} {alert['price']:,.2f}"
            body = f"Your price alert triggered.\n**Threshold** {alert['price']:,.2f}"
            if alert.get("note"):
                body += f"\n**Note** {alert['note']}"
            try:
                ok = await asyncio.to_thread(
                    discord.send_message, title, body, above
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Price alert delivery raised", exc_info=True)
                ok = False
            with database.get_conn(self._db_path) as conn:
                if ok:
                    database.mark_alert_delivered(conn, alert["id"])
                else:
                    database.bump_alert_attempts(conn, alert["id"])

    async def _deliver_alerts(self) -> None:
        """Push undelivered setups to Discord. Never fails the poll cycle.

        A setup that cannot be delivered stays queued and is retried, but only
        up to the attempt ceiling: a permanently bad webhook must not have the
        poller hammering it forever.
        """
        discord = self.discord_client
        if discord is None or not getattr(discord, "enabled", False):
            return

        with database.get_conn(self._db_path) as conn:
            pending = database.undelivered_signals(conn, self._cfg.max_alerts_per_cycle)
        if not pending:
            return

        from backend.services import backtest

        for row in pending:
            setup = _setup_from_row(row)
            # The grade never ships without its measured record.
            stats = self._grade_stats(row["ticker"], row["grade"], backtest)
            try:
                ok = await asyncio.to_thread(discord.send_setup, setup, stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Alert delivery raised", exc_info=True)
                ok = False

            with database.get_conn(self._db_path) as conn:
                if ok:
                    database.mark_signal_delivered(conn, row["id"])
                    self.alerts_sent += 1
                else:
                    database.bump_signal_attempts(conn, row["id"])

    def _grade_stats(self, ticker: str, grade: str, backtest: Any) -> dict | None:
        """Measured record for this ticker's grade, from the cache.

        Reads rather than replays: delivery runs every cycle, and a 0.4s
        replay per pending alert would put the poller to work for nothing.
        Falls back to a live replay only when the cache is empty.
        """
        try:
            with database.get_conn(self._db_path) as conn:
                cached = database.get_backtest_stats(conn, ticker)
            if cached.get(grade):
                return cached[grade]
            stats = self._run_backtest(ticker, backtest)
            hit = stats.get(grade)
            return None if hit is None else vars(hit)
        except Exception:
            # Stats are context on the alert; never let them block delivery.
            logger.warning("Could not read grade stats for %s", ticker, exc_info=True)
            return None

    async def _check_pending_links(self) -> None:
        """Validate up to the per-cycle cap of unchecked article URLs.

        Capped for the same reason enrichment is: a freshly-populated news
        table holds hundreds of links, and hammering a dozen publishers at
        once is both rude and a good way to get rate-limited into false
        "dead" verdicts.
        """
        checker = self.link_checker
        if checker is None:
            return

        with database.get_conn(self._db_path) as conn:
            pending = database.unchecked_links(conn, self._cfg.max_link_checks_per_cycle)
        if not pending:
            return

        for item in pending:
            try:
                result = await asyncio.to_thread(checker.check, item["url"] or "")
            except asyncio.CancelledError:
                raise
            except Exception:
                # LinkChecker.check is contracted never to raise; if a custom
                # one does, treat it as an unresolved check rather than a
                # verdict, so a buggy checker cannot brand links dead.
                logger.warning("Link check raised for %s", item["id"], exc_info=True)
                continue

            with database.get_conn(self._db_path) as conn:
                if not getattr(result, "final", True):
                    # The host did not answer. That is not evidence the article
                    # is gone, so retry across cycles before passing judgement.
                    attempts = database.bump_link_attempts(conn, item["id"])
                    if attempts < self._cfg.link_max_attempts:
                        continue
                database.set_link_status(conn, item["id"], result.status, result.code)

            self.links_checked += 1
            if result.status in ("dead", "error"):
                self.links_broken += 1

    async def _refresh_fundamentals(self, tickers: list[str]) -> None:
        for ticker in tickers:
            try:
                f = await asyncio.to_thread(self.client.fetch_fundamentals, ticker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Fundamentals fetch failed for %s", ticker, exc_info=True)
                continue
            if f is None:
                continue
            with database.get_conn(self._db_path) as conn:
                database.upsert_fundamentals(
                    conn, f.ticker, pe_ratio=f.pe_ratio, forward_pe=f.forward_pe,
                    market_cap=f.market_cap, eps=f.eps, revenue=f.revenue,
                    sector=f.sector, industry=f.industry,
                    dividend_yield=f.dividend_yield, beta=f.beta,
                    week52_high=f.week52_high, week52_low=f.week52_low,
                    **_extended(f),
                )

    async def _warm_one_universe_ticker(self) -> None:
        """Fetch fundamentals for a single pending universe ticker.

        Deliberately one per cycle: 500 back-to-back `.info` calls reliably
        trips Yahoo's rate limiter, and the screener is designed to work
        against partial coverage.
        """
        if self.universe_cooldown > 0:
            self.universe_cooldown -= 1
            return

        with database.get_conn(self._db_path) as conn:
            pending = database.pending_universe(conn, 1)
        if not pending:
            return
        ticker = pending[0]

        rate_limited = False
        try:
            f = await asyncio.to_thread(self.client.fetch_fundamentals, ticker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rate_limited = _is_rate_limited(exc)
            logger.warning(
                "Universe warm failed for %s%s",
                ticker,
                " (rate limited)" if rate_limited else "",
                exc_info=not rate_limited,
            )
            f = None

        if rate_limited:
            # Back off without spending an attempt: the ticker is fine, we
            # are the problem. Counting this would retire valid names after
            # three unrelated 429s -- the same bug Phase 1 shipped in
            # validate_ticker.
            self.universe_cooldown = self._cfg.universe_cooldown_cycles
            logger.info(
                "Universe warm rate-limited; pausing %d cycles",
                self.universe_cooldown,
            )
            return

        with database.get_conn(self._db_path) as conn:
            if f is None:
                attempts = database.bump_universe_attempts(conn, ticker)
                if attempts >= self._cfg.universe_max_attempts:
                    database.mark_universe_failed(conn, ticker)
                return
            database.upsert_fundamentals(
                conn, f.ticker, pe_ratio=f.pe_ratio, forward_pe=f.forward_pe,
                market_cap=f.market_cap, eps=f.eps, revenue=f.revenue,
                sector=f.sector, industry=f.industry,
                dividend_yield=f.dividend_yield, beta=f.beta,
                week52_high=f.week52_high, week52_low=f.week52_low,
                **_extended(f),
            )
            database.mark_universe_done(conn, ticker)
        self.universe_warmed += 1

    async def _refresh_yields(self) -> None:
        """The Treasury curve. Daily closes, so this is deliberately lazy."""
        import asyncio
        import json
        from datetime import datetime, timezone

        client = self.fred_client
        if client is None or not getattr(client, "enabled", False):
            return
        fetch = getattr(client, "fetch_yield_curve", None)
        if fetch is None:
            return
        try:
            points = await asyncio.to_thread(fetch)
        except Exception:
            logger.exception("macro: yield curve refresh failed")
            return
        if not points:
            return
        payload = {
            "points": [
                {"series_id": p.series_id, "label": p.label,
                 "percent": p.percent, "observed": p.observed,
                 "month_ago": p.month_ago}
                for p in points
            ],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        with database.get_conn(self._db_path) as conn:
            database.set_setting(conn, "yield_curve", json.dumps(payload))

    async def _refresh_macro(self) -> None:
        """Refresh the macro calendar. Never fails the poll cycle."""
        fred = self.fred_client
        if fred is None or not getattr(fred, "enabled", False):
            return
        try:
            events = await asyncio.to_thread(fred.fetch_upcoming)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Macro refresh failed", exc_info=True)
            # Cached events are still served, but the panel must say they may
            # be out of date rather than presenting them as current.
            self.macro_stale = True
            return
        if not events:
            self.macro_stale = True
            return
        with database.get_conn(self._db_path) as conn:
            for e in events:
                database.upsert_macro_event(
                    conn, e.id, e.release_id, e.release_name, e.event_date, e.impact
                )
        self.macro_events_seen = len(events)
        self.macro_stale = False
        self.macro_last_success = datetime.now(timezone.utc).isoformat()

    def _handle_total_failure(self, tickers: list[str]) -> bool:
        """Mark every requested ticker stale and record the failure. Returns False."""
        with database.get_conn(self._db_path) as conn:
            database.mark_stale(conn, tickers)
        self._record_failure()
        return False

    def _record_success(self) -> None:
        self.consecutive_failures = 0
        self.current_interval = self._cfg.quote_interval_seconds
        self.last_success_at = datetime.now(timezone.utc).isoformat()

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        self.current_interval = min(
            self._cfg.quote_interval_seconds * (2 ** self.consecutive_failures),
            self._cfg.backoff_ceiling_seconds,
        )


def _frames_for(conn: Any, ticker: str, horizon: Any = None) -> dict | None:
    """Build the three frames the signal engine expects, or None.

    Frames come from whichever intervals the horizon names. `4h` is the only
    non-native one and is resampled from `1h`, with an epoch origin so bucket
    edges do not drift with the fetch window.
    """
    import pandas as pd

    from backend.services import horizons as horizon_defs
    from backend.services import indicators

    spec = horizon_defs.get(None) if horizon is None else horizon

    def frame(interval: str) -> "pd.DataFrame | None":
        rows = database.get_bars(conn, ticker, interval, limit=6000)
        if len(rows) < indicators.MIN_BARS:
            return None
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
        return df[["open", "high", "low", "close", "volume"]].rename(
            columns={"open": "Open", "high": "High", "low": "Low",
                     "close": "Close", "volume": "Volume"}
        )

    out: dict = {}
    for interval in spec.frames:
        source = spec.resample.get(interval)
        if source:
            # Explicit None check: `out.get(source) or frame(source)` raises,
            # because a DataFrame has no unambiguous truth value.
            base = out.get(source)
            if base is None:
                base = frame(source)
            if base is None:
                return None
            out[interval] = indicators.resample_ohlcv(base, interval)
        else:
            built = frame(interval)
            if built is None:
                return None
            out[interval] = built
    return out


def _fingerprint(setup: Any) -> str:
    """Identity of a setup, so a persistent chart condition alerts once.

    Levels are rounded: an entry drifting by a cent is the same trade, and
    without rounding every poll cycle would look like a brand-new signal.
    """
    return "|".join([
        setup.ticker, setup.direction, setup.grade,
        f"{setup.entry:.2f}", f"{setup.stop:.2f}",
    ])


def _setup_from_row(row: dict) -> Any:
    """Rebuild a Setup-shaped object from a stored signal row.

    The Discord formatter reads attributes, and the row is a dict; rather than
    teach the formatter about rows (and couple it to the schema), the row is
    turned back into the shape the engine produced.
    """
    import json as _json
    from types import SimpleNamespace

    try:
        factors = _json.loads(row.get("factors_json") or "{}")
    except Exception:
        factors = {}
    # Deliberately NOT coerced to a tuple: the engine stores {name: passed},
    # and tuple() on that keeps only the keys -- silently turning every failed
    # factor into an apparent confluence in the delivered alert.
    if not isinstance(factors, (dict, list, tuple)):
        factors = {}
    return SimpleNamespace(
        ticker=row["ticker"], direction=row["direction"], grade=row["grade"],
        score=row["score"], factors=factors, entry=row["entry"], stop=row["stop"],
        target1=row["target1"], target2=row["target2"],
        risk_reward=row["risk_reward"], timeframes=row["timeframes"],
        earnings_at=row["earnings_at"],
    )


def _is_rate_limited(exc: Exception) -> bool:
    """True when a fetch failed because Yahoo is throttling us.

    Checked by type where the typed exception is available, and by message
    otherwise -- yfinance does not raise the typed error from every path.
    """
    from backend.services.yfinance_client import RATE_LIMIT_ERRORS

    if isinstance(exc, RATE_LIMIT_ERRORS):
        return True
    text = str(exc).lower()
    return "too many requests" in text or "rate limit" in text or "429" in text


# Extended fundamentals, forwarded by name rather than listed at each call site
# so adding a field means touching the fetcher and nothing else.
_EXTENDED_FIELDS = (
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


def _extended(f: Any) -> dict:
    return {name: getattr(f, name, None) for name in _EXTENDED_FIELDS}
