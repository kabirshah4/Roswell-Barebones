"""Curated map of FRED release names to market-impact tiers.

FRED returns ~957 release-dates a month with no impact rating of any kind, so
this map is the only thing making a usable calendar possible.

EVERY NAME BELOW WAS VERIFIED AGAINST LIVE FRED on 2026-08-12 over a 62-day
window. Do not add a name from memory — check it first, because a name that
does not match FRED's `release_name` exactly is silently dropped and nothing
surfaces the mistake. Nine of the first-draft names were wrong, including
"S&P CoreLogic Case-Shiller ..." which FRED has rebranded to "S&P Cotality ...".

Three otherwise-obvious releases are deliberately EXCLUDED because FRED
schedules them on nearly every calendar day, which makes them noise rather
than events (observed dates per 62-day window in brackets):

  - "FOMC Press Release" [36]      - a daily data container, NOT the 8x/year meeting
  - "H.15 Selected Interest Rates" [25]
  - "Commercial Paper" [25]

Excluding FOMC is a real loss — meeting dates are genuinely the highest-impact
events on any macro calendar — but FRED does not expose them as a distinct
release, and listing "FOMC Press Release" 36 times a month is worse than
omitting it. If FOMC dates are wanted later, they need a different source.

Rule of thumb for additions: a release firing more than ~12 times per 62-day
window is a data feed, not an event. Keep it out.
"""

VALID_TIERS = ("high", "medium", "low")

IMPACT: dict[str, str] = {
    # Inflation
    "Consumer Price Index": "high",
    "Personal Income and Outlays": "high",          # carries the PCE deflator
    "Producer Price Index": "medium",
    # Labour
    "Employment Situation": "high",
    "Job Openings and Labor Turnover Survey": "medium",
    "Unemployment Insurance Weekly Claims Report": "medium",
    # Growth and activity
    "Gross Domestic Product": "high",
    "Advance Monthly Sales for Retail and Food Services": "high",
    "G.17 Industrial Production and Capacity Utilization": "medium",
    "Manufacturer's Shipments, Inventories, and Orders (M3) Survey": "medium",
    "U.S. International Trade in Goods and Services": "medium",
    # Housing
    "New Residential Construction": "medium",
    "New Residential Sales": "medium",
    "S&P Cotality Case-Shiller Home Price Indices": "low",
    # Money and reserves
    "H.4.1 Factors Affecting Reserve Balances": "medium",
    "H.6 Money Stock Measures": "low",
}
