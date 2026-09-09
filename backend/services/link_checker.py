"""Validates that cached news URLs still resolve.

Headlines come from a third party and rot: articles get pulled, sites
reorganise, publishers put things behind walls. A link that 404s is worse than
no link, because the user only discovers it after clicking.

This is the third and last module permitted to import httpx, alongside
fred_client and discord_client.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# httpx logs request URLs at INFO. News URLs are not secret, but the same
# logger is shared with modules whose URLs carry credentials, so it stays quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Only genuine "this is gone" codes count as dead. A paywall, a bot-block or a
# server having a bad day is a different thing entirely, and calling it dead
# would send the user hunting for an article that is actually still there.
_DEAD_CODES = frozenset({404, 410})

# Codes that mean "ask again later", not "this is the answer". A 429 in
# particular is usually self-inflicted -- checking a batch of links from one
# publisher rate-limits us, not the articles -- so finalising it would brand a
# whole news cycle's worth of working links as problems.
_TRANSIENT_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class LinkResult:
    """A verdict on one URL.

    `final` separates "we know" from "we could not find out". A host that never
    answered may simply have been down for the ten seconds we asked, so that
    result is retried rather than written as a permanent judgement — otherwise
    one DNS blip would brand a working article broken forever.
    """

    status: str          # ok | dead | blocked | error
    code: int | None
    final: bool = True


# Measured 2026-08-26: with httpx's default User-Agent, Yahoo Finance returns
# 429 to every request regardless of pacing (serial, 0.7s apart, still 429), and
# WSJ/Barron's return 403. With this header Yahoo returns 200 and the paywalled
# sites return an honest 401. Without it the checker reports nothing useful
# about the majority of cached links, so this is load-bearing, not cosmetic.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _default_fetcher(url: str) -> int:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=10.0, headers=_HEADERS) as client:
        response = client.head(url)
        # Plenty of publishers reject HEAD outright; fall back before judging.
        if response.status_code in (403, 405, 501):
            response = client.get(url)
        return response.status_code


class LinkChecker:
    def __init__(self, fetcher: Callable[[str], Any] | None = None) -> None:
        self._fetcher = fetcher or _default_fetcher

    def check(self, url: str) -> LinkResult:
        """Classify one URL. Never raises."""
        if not url or not url.startswith(("http://", "https://")):
            # Malformed on our side; asking again would give the same answer.
            return LinkResult(status="error", code=None, final=True)
        try:
            code = int(self._fetcher(url))
        except Exception as exc:
            # Type only: an exception string can carry the full request URL.
            logger.warning("Link check failed (%s)", type(exc).__name__)
            return LinkResult(status="error", code=None, final=False)

        if code in _DEAD_CODES:
            return LinkResult(status="dead", code=code)
        if 200 <= code < 400:
            # Redirects are followed, so a 3xx here is the final hop.
            return LinkResult(status="ok", code=code)
        if code in _TRANSIENT_CODES:
            return LinkResult(status="blocked", code=code, final=False)
        # 401/403/451: the publisher answered and will keep answering this way.
        # Reported separately so the user is not told a paywalled link is fine,
        # and not told a live one is dead.
        return LinkResult(status="blocked", code=code, final=True)
