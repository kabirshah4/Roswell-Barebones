import asyncio
import time

import pytest

from backend.config import Config
from backend.db import database
from backend.services.price_poller import PricePoller
from backend.services.yfinance_client import Quote


class FakeClient:
    """Duck-typed stand-in for YFinanceClient."""

    def __init__(self, quotes=None, intraday=None):
        self._quotes = quotes if quotes is not None else {}
        self._intraday = intraday if intraday is not None else [1.0, 2.0]
        self.quote_calls = 0
        self.intraday_calls = 0

    def fetch_quotes(self, tickers):
        self.quote_calls += 1
        return {t: q for t, q in self._quotes.items() if t in tickers}

    def fetch_intraday(self, ticker):
        self.intraday_calls += 1
        return self._intraday


class RaisingQuotesClient:
    """Duck-typed client whose fetch_quotes always raises."""

    def __init__(self):
        self.quote_calls = 0

    def fetch_quotes(self, tickers):
        self.quote_calls += 1
        raise RuntimeError("simulated fetch_quotes failure")

    def fetch_intraday(self, ticker):
        raise AssertionError("fetch_intraday should not be called")


class PartialIntradayFailureClient:
    """fetch_quotes succeeds for all tickers; fetch_intraday raises for one."""

    def __init__(self, quotes, bad_ticker):
        self._quotes = quotes
        self._bad_ticker = bad_ticker
        self.intraday_calls = 0

    def fetch_quotes(self, tickers):
        return {t: q for t, q in self._quotes.items() if t in tickers}

    def fetch_intraday(self, ticker):
        self.intraday_calls += 1
        if ticker == self._bad_ticker:
            raise RuntimeError("simulated fetch_intraday failure")
        return [1.0, 2.0, 3.0]


def quote(ticker, price=100.0, prev=99.0):
    return Quote(
        ticker=ticker,
        price=price,
        prev_close=prev,
        change_pct=round((price - prev) / prev * 100, 4),
        volume=10,
        currency="USD",
    )


@pytest.fixture
def cfg(db_path):
    return Config(
        db_path=db_path,
        quote_interval_seconds=1.0,
        sparkline_every_n_cycles=3,
        backoff_ceiling_seconds=8.0,
    )


def seed(db_path, *tickers):
    with database.get_conn(db_path) as conn:
        for t in tickers:
            database.add_watchlist_ticker(conn, t)


async def test_successful_cycle_writes_cache(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(FakeClient({"AAPL": quote("AAPL", 110.0, 100.0)}), db_path, cfg)

    assert await poller.run_once() is True

    with database.get_conn(db_path) as conn:
        rows = database.get_prices(conn)
    assert len(rows) == 1
    assert rows[0]["price"] == 110.0
    assert rows[0]["change_pct"] == 10.0
    assert rows[0]["is_stale"] == 0
    assert poller.last_success_at is not None


async def test_empty_watchlist_idles_without_failure(db_path, cfg):
    poller = PricePoller(FakeClient(), db_path, cfg)
    assert await poller.run_once() is True
    assert poller.consecutive_failures == 0
    assert poller.client.quote_calls == 0
    assert poller.cycle_count == 0, "cycle_count must not increment on an empty watchlist"


async def test_total_outage_marks_stale_and_preserves_values(db_path, cfg):
    seed(db_path, "AAPL")
    good = PricePoller(FakeClient({"AAPL": quote("AAPL", 110.0, 100.0)}), db_path, cfg)
    await good.run_once()

    poller = PricePoller(FakeClient({}), db_path, cfg)
    assert await poller.run_once() is False

    with database.get_conn(db_path) as conn:
        row = database.get_prices(conn)[0]
    assert row["price"] == 110.0, "last known value must survive an outage"
    assert row["is_stale"] == 1


async def test_partial_failure_marks_only_missing_ticker_stale(db_path, cfg):
    seed(db_path, "AAPL", "MSFT")
    good = PricePoller(
        FakeClient({"AAPL": quote("AAPL"), "MSFT": quote("MSFT")}), db_path, cfg
    )
    await good.run_once()

    poller = PricePoller(FakeClient({"AAPL": quote("AAPL", 120.0, 100.0)}), db_path, cfg)
    assert await poller.run_once() is True

    with database.get_conn(db_path) as conn:
        rows = {r["ticker"]: r for r in database.get_prices(conn)}
    assert rows["AAPL"]["is_stale"] == 0
    assert rows["MSFT"]["is_stale"] == 1


async def test_backoff_grows_exponentially(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(FakeClient({}), db_path, cfg)

    await poller.run_once()
    assert poller.current_interval == 2.0
    await poller.run_once()
    assert poller.current_interval == 4.0
    await poller.run_once()
    assert poller.current_interval == 8.0


async def test_backoff_respects_ceiling(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(FakeClient({}), db_path, cfg)
    for _ in range(10):
        await poller.run_once()
    assert poller.current_interval == cfg.backoff_ceiling_seconds


async def test_recovery_resets_interval_and_clears_stale(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(FakeClient({}), db_path, cfg)
    await poller.run_once()
    await poller.run_once()
    assert poller.consecutive_failures == 2

    poller.client = FakeClient({"AAPL": quote("AAPL", 130.0, 100.0)})
    assert await poller.run_once() is True
    assert poller.consecutive_failures == 0
    assert poller.current_interval == cfg.quote_interval_seconds

    with database.get_conn(db_path) as conn:
        assert database.get_prices(conn)[0]["is_stale"] == 0


async def test_sparklines_refresh_on_cadence_not_every_cycle(db_path, cfg):
    seed(db_path, "AAPL")
    client = FakeClient({"AAPL": quote("AAPL")}, intraday=[1.0, 2.0, 3.0])
    poller = PricePoller(client, db_path, cfg)

    await poller.run_once()
    assert client.intraday_calls == 0, "must not fetch sparklines on cycle 1"
    await poller.run_once()
    assert client.intraday_calls == 0
    await poller.run_once()
    assert client.intraday_calls == 1, "cycle 3 hits the cadence"

    with database.get_conn(db_path) as conn:
        assert database.get_sparkline(conn, "AAPL") == [1.0, 2.0, 3.0]


async def test_run_loop_is_cancellable(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(FakeClient({"AAPL": quote("AAPL")}), db_path, cfg)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert poller.cycle_count >= 1


async def test_fetch_quotes_exception_is_treated_as_total_outage(db_path, cfg):
    seed(db_path, "AAPL")
    good = PricePoller(FakeClient({"AAPL": quote("AAPL", 110.0, 100.0)}), db_path, cfg)
    await good.run_once()

    poller = PricePoller(RaisingQuotesClient(), db_path, cfg)
    assert await poller.run_once() is False

    with database.get_conn(db_path) as conn:
        row = database.get_prices(conn)[0]
    assert row["price"] == 110.0, "last known value must survive a client exception"
    assert row["is_stale"] == 1
    assert poller.consecutive_failures == 1
    assert poller.current_interval == 2.0


async def test_intraday_exception_for_one_ticker_does_not_abort_others(db_path, cfg):
    seed(db_path, "AAPL", "MSFT")
    client = PartialIntradayFailureClient(
        quotes={"AAPL": quote("AAPL"), "MSFT": quote("MSFT")},
        bad_ticker="AAPL",
    )
    poller = PricePoller(client, db_path, cfg)

    await poller.run_once()
    await poller.run_once()
    assert await poller.run_once() is True, "sparkline failure must not fail the cycle"

    with database.get_conn(db_path) as conn:
        assert database.get_sparkline(conn, "AAPL") is None
        assert database.get_sparkline(conn, "MSFT") == [1.0, 2.0, 3.0]


async def test_run_loop_survives_client_exception_and_stays_cancellable(db_path, cfg):
    seed(db_path, "AAPL")
    poller = PricePoller(RaisingQuotesClient(), db_path, cfg)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert poller.cycle_count >= 1
    assert poller.consecutive_failures >= 1


from backend.services.claude_client import REFUSED, Enrichment
from backend.services.yfinance_client import Fundamentals, NewsArticle


def article(article_id, ticker="AAPL"):
    return NewsArticle(
        id=article_id, ticker=ticker, title=f"Title {article_id}",
        publisher="Reuters", url="http://x", published_at="2026-08-10T00:00:00Z",
        summary="Blurb",
    )


class NewsClient(FakeClient):
    def __init__(self, quotes=None, news=None, fundamentals=None):
        super().__init__(quotes)
        self._news = news or []
        self._fundamentals = fundamentals
        self.news_calls = 0
        self.fundamentals_calls = 0

    def fetch_news(self, ticker):
        self.news_calls += 1
        return self._news

    def fetch_fundamentals(self, ticker):
        self.fundamentals_calls += 1
        return self._fundamentals


class FakeAI:
    def __init__(self, enabled=True, result=None):
        self.enabled = enabled
        self._result = result or Enrichment("A one-liner.", "bullish")
        self.calls = 0

    def enrich(self, title, summary):
        self.calls += 1
        return self._result


@pytest.fixture
def news_cfg(db_path):
    return Config(
        db_path=db_path, quote_interval_seconds=1.0, sparkline_every_n_cycles=99,
        news_every_n_cycles=2, fundamentals_every_n_cycles=3,
        max_enrichments_per_cycle=2,
    )


async def test_news_fetched_on_cadence_only(db_path, news_cfg):
    seed(db_path, "AAPL")
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=FakeAI())

    await poller.run_once()
    assert client.news_calls == 0, "must not fetch news on cycle 1"
    await poller.run_once()
    assert client.news_calls == 1, "cycle 2 hits the cadence"

    with database.get_conn(db_path) as conn:
        assert len(database.list_news(conn, "AAPL")) == 1


async def test_enrichment_runs_and_persists(db_path, news_cfg):
    seed(db_path, "AAPL")
    ai = FakeAI(result=Enrichment("Apple slipped.", "bearish"))
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    await poller.run_once()
    await poller.run_once()

    with database.get_conn(db_path) as conn:
        row = database.list_news(conn, "AAPL")[0]
    assert row["ai_summary"] == "Apple slipped."
    assert row["sentiment"] == "bearish"
    assert row["enrich_state"] == "done"


async def test_article_is_never_enriched_twice(db_path, news_cfg):
    """The whole point of the cache: we pay for each article exactly once."""
    seed(db_path, "AAPL")
    ai = FakeAI()
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    for _ in range(6):
        await poller.run_once()

    assert ai.calls == 1, f"expected exactly one enrichment call, got {ai.calls}"


async def test_enrichment_respects_per_cycle_cap(db_path, news_cfg):
    seed(db_path, "AAPL")
    ai = FakeAI()
    many = [article(f"a{i}") for i in range(5)]
    client = NewsClient({"AAPL": quote("AAPL")}, news=many)
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    await poller.run_once()
    await poller.run_once()
    assert ai.calls == 2, "cap is 2 per cycle"


async def test_no_ai_client_marks_articles_skipped(db_path, news_cfg):
    seed(db_path, "AAPL")
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=FakeAI(enabled=False))

    await poller.run_once()
    await poller.run_once()

    with database.get_conn(db_path) as conn:
        row = database.list_news(conn, "AAPL")[0]
    assert row["enrich_state"] == "skipped"
    assert row["ai_summary"] is None


async def test_ai_failure_does_not_break_the_cycle(db_path, news_cfg):
    seed(db_path, "AAPL")
    ai = FakeAI(result=None)
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    await poller.run_once()
    assert await poller.run_once() is True, "an AI failure must not fail the poll cycle"


async def test_ai_exception_does_not_escape(db_path, news_cfg):
    class Exploding:
        enabled = True

        def enrich(self, title, summary):
            raise RuntimeError("provider down")

    seed(db_path, "AAPL")
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=Exploding())

    await poller.run_once()
    assert await poller.run_once() is True


async def test_fundamentals_fetched_on_own_cadence(db_path, news_cfg):
    seed(db_path, "AAPL")
    f = Fundamentals(
        ticker="AAPL", pe_ratio=35.9, forward_pe=32.4, market_cap=4.5e12, eps=8.57,
        revenue=4.6e11, sector="Technology", industry="Consumer Electronics",
        dividend_yield=0.35, beta=1.086, week52_high=344.57, week52_low=223.78,
    )
    client = NewsClient({"AAPL": quote("AAPL")}, fundamentals=f)
    poller = PricePoller(client, db_path, news_cfg, ai_client=FakeAI())

    await poller.run_once()
    await poller.run_once()
    assert client.fundamentals_calls == 0, "cadence is 3"
    await poller.run_once()
    assert client.fundamentals_calls == 1

    with database.get_conn(db_path) as conn:
        stored = database.get_fundamentals(conn, "AAPL")
    assert stored["pe_ratio"] == 35.9
    assert stored["sector"] == "Technology"


async def test_poller_works_with_no_ai_client_at_all(db_path, news_cfg):
    """ai_client defaults to None — the constructor must not require one."""
    seed(db_path, "AAPL")
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg)
    await poller.run_once()
    assert await poller.run_once() is True


async def test_enrichment_stops_after_max_attempts(db_path, news_cfg):
    """Retries are capped at 3; after that the article is `failed` and never
    billed again, even across many more poll cycles.

    Uses a dedicated always-failing stub rather than ``FakeAI(result=None)``:
    ``FakeAI.__init__`` does ``self._result = result or Enrichment(...)``, so
    passing ``result=None`` is swallowed by ``or`` and it actually returns a
    successful Enrichment, not None. That stub cannot exercise this path.
    """

    class AlwaysFailingAI:
        enabled = True

        def __init__(self):
            self.calls = 0

        def enrich(self, title, summary):
            self.calls += 1
            return None

    seed(db_path, "AAPL")
    ai = AlwaysFailingAI()
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    # news cadence is every 2 cycles, so 6 cycles = 3 news-fetch/enrich passes.
    for _ in range(6):
        await poller.run_once()

    with database.get_conn(db_path) as conn:
        row = database.list_news(conn, "AAPL")[0]
    assert row["enrich_state"] == "failed"
    assert row["enrich_attempts"] == 3
    assert ai.calls == 3

    # More cadence passes must not re-attempt a failed article.
    for _ in range(4):
        await poller.run_once()
    assert ai.calls == 3, "must stop calling the AI once an article is failed"


async def test_refusal_marks_failed_immediately_without_retry(db_path, news_cfg):
    """A refusal is deterministic for the same input, so it must be routed
    straight to `failed` on the first attempt instead of burning the generic
    3-attempt retry budget (design doc §8). `enrich_attempts` must stay 0
    since the counter tracks *retryable* failures, and the AI must never be
    called again for this article on later cycles.
    """

    class RefusingAI:
        enabled = True

        def __init__(self):
            self.calls = 0

        def enrich(self, title: str, summary: str) -> object:
            self.calls += 1
            return REFUSED

    seed(db_path, "AAPL")
    ai = RefusingAI()
    client = NewsClient({"AAPL": quote("AAPL")}, news=[article("a1")])
    poller = PricePoller(client, db_path, news_cfg, ai_client=ai)

    # news cadence is every 2 cycles; the second run_once is the first pass
    # that fetches news and attempts enrichment.
    await poller.run_once()
    await poller.run_once()

    with database.get_conn(db_path) as conn:
        row = database.list_news(conn, "AAPL")[0]
    assert row["enrich_state"] == "failed"
    assert row["enrich_attempts"] == 0
    assert ai.calls == 1

    for _ in range(4):
        await poller.run_once()
    assert ai.calls == 1, "a refused article must never be retried"


@pytest.fixture
def cancel_cfg(db_path):
    return Config(
        db_path=db_path, quote_interval_seconds=1.0, sparkline_every_n_cycles=99,
        news_every_n_cycles=1, fundamentals_every_n_cycles=99,
        max_enrichments_per_cycle=2,
    )


async def test_cancellation_mid_news_fetch_propagates_and_stops_the_loop(
    db_path, cancel_cfg
):
    """A CancelledError raised while blocked inside `_refresh_news`'s
    `asyncio.to_thread` call must propagate out of `run()`, not be swallowed --
    otherwise the poller becomes unstoppable on shutdown.
    """

    class SlowNewsClient(FakeClient):
        def fetch_news(self, ticker):
            time.sleep(0.3)
            return []

    seed(db_path, "AAPL")
    client = SlowNewsClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, cancel_cfg, ai_client=FakeAI())
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)  # cycle 1 is now blocked inside fetch_news
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


from backend.services.fred_client import MacroEvent


class FakeFred:
    def __init__(self, enabled=True, events=None, raises=None):
        self.enabled = enabled
        self._events = events or []
        self._raises = raises
        self.calls = 0

    def fetch_upcoming(self, days=30):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._events


class UniverseClient(NewsClient):
    def __init__(self, quotes=None, fundamentals=None, fail_on=()):
        super().__init__(quotes, fundamentals=fundamentals)
        self._fail_on = set(fail_on)
        self.warmed = []

    def fetch_fundamentals(self, ticker):
        self.fundamentals_calls += 1
        self.warmed.append(ticker)
        if ticker in self._fail_on:
            return None
        return self._fundamentals


@pytest.fixture
def uni_cfg(db_path):
    return Config(
        db_path=db_path, quote_interval_seconds=1.0, sparkline_every_n_cycles=99,
        news_every_n_cycles=99, fundamentals_every_n_cycles=99,
        macro_every_n_cycles=2, universe_max_attempts=3,
    )


def fundamentals_for(ticker="X"):
    return Fundamentals(
        ticker=ticker, pe_ratio=20.0, forward_pe=18.0, market_cap=1e11, eps=5.0,
        revenue=1e10, sector="Technology", industry="Software",
        dividend_yield=1.0, beta=1.0, week52_high=100.0, week52_low=50.0,
    )


async def test_universe_warms_one_ticker_per_cycle(db_path, uni_cfg):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["AAA", "BBB", "CCC"])
    seed(db_path, "AAPL")
    client = UniverseClient({"AAPL": quote("AAPL")}, fundamentals=fundamentals_for())
    poller = PricePoller(client, db_path, uni_cfg)

    await poller.run_once()
    assert len(client.warmed) == 1, "exactly one universe ticker per cycle"
    await poller.run_once()
    assert len(client.warmed) == 2


async def test_warmed_ticker_is_not_rewarmed(db_path, uni_cfg):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["AAA"])
    seed(db_path, "AAPL")
    client = UniverseClient({"AAPL": quote("AAPL")}, fundamentals=fundamentals_for())
    poller = PricePoller(client, db_path, uni_cfg)

    for _ in range(5):
        await poller.run_once()
    assert client.warmed == ["AAA"], "a done ticker must never be refetched"


async def test_failed_universe_ticker_gives_up_after_max_attempts(db_path, uni_cfg):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["BAD"])
    seed(db_path, "AAPL")
    client = UniverseClient({"AAPL": quote("AAPL")}, fail_on=("BAD",))
    poller = PricePoller(client, db_path, uni_cfg)

    for _ in range(8):
        await poller.run_once()
    assert len(client.warmed) == 3, "one delisted name must not stall the queue forever"
    with database.get_conn(db_path) as conn:
        assert database.pending_universe(conn, 10) == []


async def test_empty_universe_is_a_noop(db_path, uni_cfg):
    seed(db_path, "AAPL")
    client = UniverseClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, uni_cfg)
    assert await poller.run_once() is True
    assert client.warmed == []


async def test_macro_refreshes_on_cadence(db_path, uni_cfg):
    seed(db_path, "AAPL")
    fred = FakeFred(events=[MacroEvent("1|2099-01-01", 1, "Consumer Price Index",
                                       "2099-01-01", "high")])
    client = UniverseClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, uni_cfg, fred_client=fred)

    await poller.run_once()
    assert fred.calls == 0, "cadence is 2"
    await poller.run_once()
    assert fred.calls == 1

    with database.get_conn(db_path) as conn:
        events = database.list_macro_events(conn)
    assert [e["release_name"] for e in events] == ["Consumer Price Index"]


async def test_disabled_fred_is_never_called(db_path, uni_cfg):
    seed(db_path, "AAPL")
    fred = FakeFred(enabled=False)
    client = UniverseClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, uni_cfg, fred_client=fred)

    await poller.run_once()
    await poller.run_once()
    assert fred.calls == 0, "no key means zero FRED calls, not failed ones"


async def test_fred_exception_does_not_fail_the_cycle(db_path, uni_cfg):
    seed(db_path, "AAPL")
    fred = FakeFred(raises=RuntimeError("fred down"))
    client = UniverseClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, uni_cfg, fred_client=fred)

    await poller.run_once()
    assert await poller.run_once() is True


async def test_poller_works_with_no_fred_client(db_path, uni_cfg):
    seed(db_path, "AAPL")
    client = UniverseClient({"AAPL": quote("AAPL")})
    poller = PricePoller(client, db_path, uni_cfg)
    await poller.run_once()
    assert await poller.run_once() is True


# --- watchlist-independent work regressions --------------------------------
# The screener and macro calendar must keep filling in before the user has
# added any ticker. A Phase 1 early-return once skipped both entirely.

async def test_universe_warms_with_an_empty_watchlist(db_path, uni_cfg):
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["AAA", "BBB", "CCC"])
    client = UniverseClient({}, fundamentals=fundamentals_for())
    poller = PricePoller(client, db_path, uni_cfg)

    for _ in range(3):
        assert await poller.run_once() is True

    assert len(client.warmed) == 3, "universe must warm without a watchlist"


async def test_macro_refreshes_with_an_empty_watchlist(db_path, uni_cfg):
    fred = FakeFred(events=[MacroEvent("1|2099-01-01", 1, "Consumer Price Index",
                                       "2099-01-01", "high")])
    client = UniverseClient({})
    poller = PricePoller(client, db_path, uni_cfg, fred_client=fred)

    await poller.run_once()
    await poller.run_once()
    assert fred.calls >= 1, "macro cadence must advance without a watchlist"

    with database.get_conn(db_path) as conn:
        assert len(database.list_macro_events(conn)) == 1


async def test_empty_watchlist_still_leaves_cycle_count_at_zero(db_path, uni_cfg):
    """cycle_count counts watchlist cycles only; total_cycles drives the rest."""
    with database.get_conn(db_path) as conn:
        database.seed_universe(conn, ["AAA"])
    client = UniverseClient({}, fundamentals=fundamentals_for())
    poller = PricePoller(client, db_path, uni_cfg)

    for _ in range(3):
        await poller.run_once()

    assert poller.cycle_count == 0
    assert poller.total_cycles == 3


# --- the watchlist sparkline -------------------------------------------------

def test_the_sparkline_reaches_back_a_session():
    """At 09:41 a single day has produced three 5-minute bars, and a
    three-point sparkline is a straight line — which is exactly what the trend
    column showed every morning. Two days guarantees a shape at any hour.
    """
    import pandas as pd

    from backend.services.yfinance_client import SPARKLINE_POINTS, YFinanceClient

    asked = {}

    class Frame:
        empty = False
        def __init__(self, n): self._n = n
        def __getitem__(self, key):
            return pd.Series([100.0 + i for i in range(self._n)])

    class Ticker:
        def history(self, period, interval):
            asked["period"] = period
            asked["interval"] = interval
            # One 5-minute bar a minute of the session, two sessions' worth.
            return Frame(160)

    client = YFinanceClient(ticker_factory=lambda t: Ticker())
    points = client.fetch_intraday("AAPL")

    assert asked["period"] == "2d", "one day is three bars at the open"
    assert asked["interval"] == "5m"
    assert len(points) == SPARKLINE_POINTS


def test_the_sparkline_keeps_the_most_recent_bars():
    """Trimmed from the tail, not the head — a trend line ending an hour ago
    would be quietly wrong rather than obviously broken."""
    import pandas as pd

    from backend.services.yfinance_client import SPARKLINE_POINTS, YFinanceClient

    class Frame:
        empty = False
        def __getitem__(self, key):
            return pd.Series([float(i) for i in range(200)])

    class Ticker:
        def history(self, period, interval): return Frame()

    points = YFinanceClient(ticker_factory=lambda t: Ticker()).fetch_intraday("X")
    assert points[-1] == 199.0
    assert points[0] == float(200 - SPARKLINE_POINTS)


def test_a_short_history_is_returned_whole():
    """A newly listed name has less than a session. Better a short line than
    none."""
    import pandas as pd

    from backend.services.yfinance_client import YFinanceClient

    class Frame:
        empty = False
        def __getitem__(self, key): return pd.Series([1.0, 2.0, 3.0])

    class Ticker:
        def history(self, period, interval): return Frame()

    assert YFinanceClient(ticker_factory=lambda t: Ticker()).fetch_intraday("X") == [1.0, 2.0, 3.0]
