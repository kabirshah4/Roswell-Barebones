"""The poller is the only component allowed to check links; routes must not."""

import asyncio

from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services.link_checker import LinkResult
from backend.services.price_poller import PricePoller


class Quote:
    def __init__(self):
        self.price = 1.0
        self.prev_close = 1.0
        self.change_pct = 0.0
        self.volume = 1
        self.currency = "USD"


class Article:
    def __init__(self, aid, url):
        self.id = aid
        self.ticker = "AAPL"
        self.title = f"T{aid}"
        self.publisher = "Reuters"
        self.url = url
        self.published_at = "2026-08-01T00:00:00Z"
        self.summary = "blurb"


class FakeYF:
    def __init__(self, articles=()):
        self.articles = list(articles)

    def fetch_quotes(self, tickers):
        return {t: Quote() for t in tickers}

    def fetch_intraday(self, ticker):
        return []

    def fetch_news(self, ticker):
        return self.articles

    def fetch_fundamentals(self, ticker):
        return None


class RecordingChecker:
    def __init__(self, verdicts=None):
        self.seen = []
        self._verdicts = verdicts or {}

    def check(self, url):
        self.seen.append(url)
        return self._verdicts.get(url, LinkResult("ok", 200))


def make_poller(db_path, checker, articles=(), cfg=None):
    cfg = cfg or Config(db_path=db_path, news_every_n_cycles=1)
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
    return PricePoller(
        FakeYF(articles), db_path, cfg, ai_client=None, link_checker=checker
    )


def test_poller_checks_new_article_links(db_path):
    checker = RecordingChecker()
    poller = make_poller(db_path, checker, [Article("a1", "https://x.test/1")])
    asyncio.run(poller.run_once())
    assert checker.seen == ["https://x.test/1"]
    assert poller.links_checked == 1


def test_verdict_is_persisted(db_path):
    url = "https://x.test/gone"
    checker = RecordingChecker({url: LinkResult("dead", 404)})
    poller = make_poller(db_path, checker, [Article("a1", url)])
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        broken = database.broken_links(conn)
    assert [(r["id"], r["link_status"], r["link_code"]) for r in broken] == [
        ("a1", "dead", 404)
    ]
    assert poller.links_broken == 1


def test_links_are_checked_once_not_every_cycle(db_path):
    checker = RecordingChecker()
    poller = make_poller(db_path, checker, [Article("a1", "https://x.test/1")])
    asyncio.run(poller.run_once())
    asyncio.run(poller.run_once())
    assert checker.seen == ["https://x.test/1"]


def test_per_cycle_cap_is_respected(db_path):
    articles = [Article(f"a{i}", f"https://x.test/{i}") for i in range(10)]
    cfg = Config(db_path=db_path, news_every_n_cycles=1, max_link_checks_per_cycle=3)
    checker = RecordingChecker()
    poller = make_poller(db_path, checker, articles, cfg=cfg)
    asyncio.run(poller.run_once())
    assert len(checker.seen) == 3


def test_no_checker_configured_is_a_noop(db_path):
    poller = make_poller(db_path, None, [Article("a1", "https://x.test/1")])
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.link_summary(conn) == {"unchecked": 1}


def test_a_raising_checker_leaves_the_link_unchecked(db_path):
    """A buggy checker must not be able to brand a working link dead."""

    class Boom:
        def check(self, url):
            raise RuntimeError("bug")

    poller = make_poller(db_path, Boom(), [Article("a1", "https://x.test/1")])
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.link_summary(conn) == {"unchecked": 1}
        assert database.broken_links(conn) == []


def test_error_verdict_counts_as_broken(db_path):
    url = "https://x.test/dns"
    checker = RecordingChecker({url: LinkResult("error", None)})
    poller = make_poller(db_path, checker, [Article("a1", url)])
    asyncio.run(poller.run_once())
    assert poller.links_broken == 1


def test_ok_verdict_is_not_broken(db_path):
    checker = RecordingChecker()
    poller = make_poller(db_path, checker, [Article("a1", "https://x.test/1")])
    asyncio.run(poller.run_once())
    assert poller.links_broken == 0


# --- routes never check links ----------------------------------------------

class ExplodingChecker:
    def check(self, url):
        raise AssertionError("routes must never check links")


class ExplodingAI:
    enabled = True

    def enrich(self, title, summary):
        raise AssertionError("routes must never call the AI provider")


class ExplodingYF:
    def fetch_quotes(self, tickers):
        raise AssertionError("routes must never fetch")

    def fetch_intraday(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_news(self, ticker):
        raise AssertionError("routes must never fetch")

    def fetch_fundamentals(self, ticker):
        raise AssertionError("routes must never fetch")

    def validate_ticker(self, ticker):
        return True


def test_news_route_serves_link_status_without_checking(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_news_article(
            conn, "a1", "AAPL", "T", "Reuters", "https://x.test/1",
            "2026-08-01T00:00:00Z", "blurb")
        database.set_link_status(conn, "a1", "dead", 404)

    app = create_app(
        cfg=Config(db_path=db_path), client=ExplodingYF(), ai_client=ExplodingAI(),
        link_checker=ExplodingChecker(), start_poller=False,
    )
    with TestClient(app) as c:
        article = c.get("/api/news/AAPL").json()["articles"][0]
    assert article["link_status"] == "dead"
    assert article["link_code"] == 404


# --- inconclusive checks are retried, not written as verdicts ----------------

def test_unreachable_host_is_retried_before_being_called_broken(db_path):
    """One outage must not permanently brand a working article broken."""
    url = "https://x.test/flaky"
    checker = RecordingChecker({url: LinkResult("error", None, final=False)})
    cfg = Config(db_path=db_path, news_every_n_cycles=1, link_max_attempts=3)
    poller = make_poller(db_path, checker, [Article("a1", url)], cfg=cfg)

    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert database.broken_links(conn) == []
        assert database.link_summary(conn) == {"unchecked": 1}
    assert poller.links_broken == 0


def test_it_gives_up_after_the_attempt_ceiling(db_path):
    url = "https://x.test/flaky"
    checker = RecordingChecker({url: LinkResult("error", None, final=False)})
    cfg = Config(db_path=db_path, news_every_n_cycles=1, link_max_attempts=3)
    poller = make_poller(db_path, checker, [Article("a1", url)], cfg=cfg)

    for _ in range(3):
        asyncio.run(poller.run_once())

    with database.get_conn(db_path) as conn:
        assert [r["id"] for r in database.broken_links(conn)] == ["a1"]
    assert poller.links_broken == 1


def test_a_recovered_host_is_recorded_ok_not_broken(db_path):
    """The retry must actually be able to change the verdict."""
    url = "https://x.test/flaky"
    checker = RecordingChecker({url: LinkResult("error", None, final=False)})
    cfg = Config(db_path=db_path, news_every_n_cycles=1, link_max_attempts=3)
    poller = make_poller(db_path, checker, [Article("a1", url)], cfg=cfg)

    asyncio.run(poller.run_once())
    checker._verdicts[url] = LinkResult("ok", 200)
    asyncio.run(poller.run_once())

    with database.get_conn(db_path) as conn:
        assert database.broken_links(conn) == []
        assert database.link_summary(conn) == {"ok": 1}


def test_blocked_is_final_on_the_first_look(db_path):
    """The publisher answered — there is nothing to retry."""
    url = "https://x.test/paywall"
    checker = RecordingChecker({url: LinkResult("blocked", 403)})
    poller = make_poller(db_path, checker, [Article("a1", url)])
    asyncio.run(poller.run_once())

    with database.get_conn(db_path) as conn:
        assert database.link_summary(conn) == {"blocked": 1}
        assert database.broken_links(conn) == []
    assert poller.links_checked == 1
    assert poller.links_broken == 0


def test_a_malformed_url_is_final_immediately(db_path):
    checker = RecordingChecker({"javascript:x": LinkResult("error", None, final=True)})
    poller = make_poller(db_path, checker, [Article("a1", "javascript:x")])
    asyncio.run(poller.run_once())
    with database.get_conn(db_path) as conn:
        assert [r["id"] for r in database.broken_links(conn)] == ["a1"]
