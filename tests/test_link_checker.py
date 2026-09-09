from backend.db import database
from backend.services.link_checker import LinkChecker, LinkResult


def fetcher(code):
    def f(url):
        return code

    return f


def raising(exc):
    def f(url):
        raise exc

    return f


# --- classification ---------------------------------------------------------

def test_200_is_ok():
    r = LinkChecker(fetcher(200)).check("https://x.test/a")
    assert (r.status, r.code, r.final) == ("ok", 200, True)


def test_301_is_ok_because_redirects_are_followed():
    assert LinkChecker(fetcher(301)).check("https://x.test/a").status == "ok"


def test_404_is_dead():
    r = LinkChecker(fetcher(404)).check("https://x.test/a")
    assert (r.status, r.code, r.final) == ("dead", 404, True)


def test_410_is_dead():
    assert LinkChecker(fetcher(410)).check("https://x.test/a").status == "dead"


def test_403_is_blocked_not_dead():
    """A paywall or bot-block is the publisher's choice, not a broken article."""
    assert LinkChecker(fetcher(403)).check("https://x.test/a").status == "blocked"


def test_429_is_blocked_not_dead():
    assert LinkChecker(fetcher(429)).check("https://x.test/a").status == "blocked"


def test_429_is_retryable_because_it_is_usually_our_own_fault():
    """Checking many links from one publisher rate-limits us, not the articles."""
    assert LinkChecker(fetcher(429)).check("https://x.test/a").final is False


def test_500_is_blocked_not_dead():
    """A server having a bad day should not be reported as a missing article."""
    assert LinkChecker(fetcher(500)).check("https://x.test/a").status == "blocked"


def test_5xx_is_retryable():
    for code in (500, 502, 503, 504):
        assert LinkChecker(fetcher(code)).check("https://x.test/a").final is False


def test_403_stays_final_because_a_bot_block_does_not_lift():
    """HEAD already fell back to GET; a second 403 is a stable answer."""
    assert LinkChecker(fetcher(403)).check("https://x.test/a").final is True


def test_401_is_final_blocked():
    r = LinkChecker(fetcher(401)).check("https://x.test/a")
    assert (r.status, r.final) == ("blocked", True)


def test_blocked_keeps_the_code_so_the_reason_is_visible():
    assert LinkChecker(fetcher(451)).check("https://x.test/a").code == 451


def test_blocked_keeps_its_status_across_the_final_split():
    """Both retryable and permanent blocks are the same status to the reader."""
    assert LinkChecker(fetcher(403)).check("https://x.test/a").status == "blocked"
    assert LinkChecker(fetcher(503)).check("https://x.test/a").status == "blocked"


def test_network_failure_is_error_not_dead():
    result = LinkChecker(raising(RuntimeError("dns"))).check("https://x.test/a")
    assert result.status == "error"
    assert result.code is None


def test_network_failure_is_not_final_so_it_gets_retried():
    """One DNS blip must not permanently brand a working link broken."""
    assert LinkChecker(raising(RuntimeError("dns"))).check("https://x.test/a").final is False


def test_malformed_url_is_a_final_error():
    """Nothing about retrying a javascript: URL will change the answer."""
    assert LinkChecker(fetcher(200)).check("javascript:alert(1)").final is True


def test_error_text_is_never_logged(caplog):
    """A request exception can embed the full URL, tokens and all."""
    with caplog.at_level("DEBUG"):
        LinkChecker(raising(RuntimeError("GET https://x.test/a?token=SEEKRIT"))).check(
            "https://x.test/a")
    assert "SEEKRIT" not in caplog.text


def test_empty_url_is_error():
    assert LinkChecker(fetcher(200)).check("").status == "error"


def test_non_http_scheme_is_error():
    assert LinkChecker(fetcher(200)).check("javascript:alert(1)").status == "error"


def test_check_never_raises():
    for bad in (None, "", "notaurl", "ftp://x.test"):
        assert LinkChecker(raising(ValueError("x"))).check(bad or "").status == "error"


# --- storage ----------------------------------------------------------------

def _article(conn, aid, url="https://x.test/a"):
    database.upsert_news_article(
        conn, aid, "AAPL", f"Title {aid}", "Reuters", url,
        "2026-08-01T00:00:00Z", "blurb")


def test_new_articles_start_unchecked(conn):
    _article(conn, "a1")
    assert [r["id"] for r in database.unchecked_links(conn)] == ["a1"]


def test_checked_articles_leave_the_queue(conn):
    _article(conn, "a1")
    database.set_link_status(conn, "a1", "ok", 200)
    assert database.unchecked_links(conn) == []


def test_articles_without_a_url_are_not_queued(conn):
    _article(conn, "a1", url="")
    assert database.unchecked_links(conn) == []


def test_broken_links_lists_dead_and_error(conn):
    _article(conn, "ok1"); _article(conn, "dead1"); _article(conn, "err1")
    database.set_link_status(conn, "ok1", "ok", 200)
    database.set_link_status(conn, "dead1", "dead", 404)
    database.set_link_status(conn, "err1", "error", None)
    assert {r["id"] for r in database.broken_links(conn)} == {"dead1", "err1"}


def test_blocked_is_not_reported_as_broken(conn):
    """A paywalled article still exists; listing it as broken is a false alarm."""
    _article(conn, "pay1")
    database.set_link_status(conn, "pay1", "blocked", 403)
    assert database.broken_links(conn) == []


def test_blocked_still_shows_in_the_summary(conn):
    _article(conn, "pay1")
    database.set_link_status(conn, "pay1", "blocked", 403)
    assert database.link_summary(conn)["blocked"] == 1


def test_bump_link_attempts_counts_up_and_keeps_the_row_queued(conn):
    _article(conn, "a1")
    assert database.bump_link_attempts(conn, "a1") == 1
    assert database.bump_link_attempts(conn, "a1") == 2
    assert [r["id"] for r in database.unchecked_links(conn)] == ["a1"]


def test_link_summary_counts_each_status(conn):
    _article(conn, "a1"); _article(conn, "a2")
    database.set_link_status(conn, "a1", "dead", 404)
    summary = database.link_summary(conn)
    assert summary["dead"] == 1
    assert summary["unchecked"] == 1


def test_migration_is_idempotent(db_path):
    """init_db runs on every startup; the added columns must not error twice."""
    database.init_db(db_path)
    database.init_db(db_path)
    with database.get_conn(db_path) as conn:
        assert database.link_summary(conn) == {}


def test_the_real_fetcher_sends_a_browser_user_agent():
    """Load-bearing: with httpx's default UA, Yahoo 429s every link we cache."""
    from backend.services import link_checker

    ua = link_checker._HEADERS["User-Agent"]
    assert "Mozilla" in ua and "python" not in ua.lower()
