"""Which US equity session we are in.

The scanner behaves differently before the open than after it: pre-market
ranks on the gap and the pre-market volume behind it, while the regular
session ranks on relative volume and the move so far. Getting the session
wrong means ranking on a field that is stale or empty.

Pure functions over an injected clock, so the behaviour is testable at any
hour rather than only when the suite happens to run.
"""

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# US equities. Pre-market quotes exist from 04:00 but are thin before ~07:00.
PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)


@dataclass(frozen=True)
class Session:
    name: str      # premarket | open | afterhours | closed | weekend
    label: str
    is_tradeable: bool
    ranks_on: str  # which field the scanner should sort by


SESSIONS = {
    "premarket": Session("premarket", "PRE-MARKET", True, "premarket_change"),
    "open": Session("open", "MARKET OPEN", True, "change"),
    "afterhours": Session("afterhours", "AFTER HOURS", True, "change"),
    "closed": Session("closed", "CLOSED", False, "change"),
    "weekend": Session("weekend", "WEEKEND", False, "change"),
}


def current(now: datetime | None = None) -> Session:
    """The session right now, in Eastern time.

    Holidays are deliberately not modelled: the scanner reads live quotes, so
    a holiday simply returns a stale, unchanged list rather than wrong data,
    and a hand-maintained holiday table would be one more thing to go stale.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    if moment.weekday() >= 5:
        return SESSIONS["weekend"]

    clock = moment.time()
    if PREMARKET_OPEN <= clock < REGULAR_OPEN:
        return SESSIONS["premarket"]
    if REGULAR_OPEN <= clock < REGULAR_CLOSE:
        return SESSIONS["open"]
    if REGULAR_CLOSE <= clock < AFTER_HOURS_CLOSE:
        return SESSIONS["afterhours"]
    return SESSIONS["closed"]
