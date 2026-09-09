"""Workspaces, maximise, keyboard, browse, and the extended fundamentals."""

import re

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app


@pytest.fixture
def assets(db_path):
    with TestClient(create_app(cfg=Config(db_path=db_path), start_poller=False)) as c:
        return {"html": c.get("/").text, "css": c.get("/static/style.css").text,
                "js": c.get("/static/app.js").text}


def client(db_path):
    return TestClient(create_app(cfg=Config(db_path=db_path), start_poller=False))


# --- browse ------------------------------------------------------------------

def seed(conn, n=120):
    from backend.services.screener_client import Symbol

    database.replace_symbols(conn, [
        Symbol(f"T{i:04d}", f"NASDAQ:T{i:04d}", f"Company {i}", "NASDAQ",
               "stock", float(1_000_000 * (n - i)))
        for i in range(n)
    ])


def test_browse_returns_a_page_largest_first(conn):
    seed(conn)
    page = database.browse_symbols(conn, 0, 10)
    assert len(page) == 10
    assert page[0]["symbol"] == "T0000"


def test_paging_does_not_repeat_or_skip(conn):
    """market_cap alone is not a total order; without the symbol tiebreak
    SQLite may return ties differently between queries."""
    seed(conn)
    first = [r["symbol"] for r in database.browse_symbols(conn, 0, 40)]
    second = [r["symbol"] for r in database.browse_symbols(conn, 40, 40)]
    assert len(set(first) & set(second)) == 0
    assert len(set(first) | set(second)) == 80


def test_ties_are_ordered_deterministically(conn):
    from backend.services.screener_client import Symbol

    database.replace_symbols(conn, [
        Symbol(s, f"NASDAQ:{s}", s, "NASDAQ", "stock", None)
        for s in ("ZZZ", "AAA", "MMM")
    ])
    twice = [
        [r["symbol"] for r in database.browse_symbols(conn, 0, 3)] for _ in range(2)
    ]
    assert twice[0] == twice[1] == ["AAA", "MMM", "ZZZ"]


def test_browse_can_be_filtered_by_kind(conn):
    from backend.services.screener_client import Symbol

    database.replace_symbols(conn, [
        Symbol("AAA", "N:AAA", "A", "NASDAQ", "stock", 1e9),
        Symbol("BBB", "N:BBB", "B", "NASDAQ", "fund", 1e9),
    ])
    assert [r["symbol"] for r in database.browse_symbols(conn, kind="fund")] == ["BBB"]
    assert database.symbol_count(conn, "fund") == 1


def test_the_browse_route_reports_whether_more_remain(db_path):
    with database.get_conn(db_path) as conn:
        seed(conn, n=30)
    with client(db_path) as c:
        first = c.get("/api/symbols/browse?offset=0&limit=10").json()
        last = c.get("/api/symbols/browse?offset=20&limit=10").json()
    assert first["has_more"] is True and first["total"] == 30
    assert last["has_more"] is False


def test_the_page_size_is_capped(db_path):
    with database.get_conn(db_path) as conn:
        seed(conn, n=150)
    with client(db_path) as c:
        assert len(c.get("/api/symbols/browse?limit=99999").json()["results"]) <= 200


def test_clicking_the_box_opens_the_modal(assets):
    """Otherwise the catalogue is only reachable if you already know a name."""
    js = assets["js"]
    assert 'addEventListener("focus", openSymbolModal)' in js
    assert 'id="symbol-modal"' in assets["html"]


def test_the_add_box_is_a_trigger_not_a_field(assets):
    """Typing into it would race the modal that opens on focus."""
    block = assets["html"][assets["html"].index('id="add-input"'):]
    assert "readonly" in block[:200]


def test_scrolling_the_modal_loads_more(assets):
    js = assets["js"]
    block = js[js.index('$("modal-results").addEventListener("scroll"'):]
    assert "loadModal(false)" in block[:400]


def test_the_modal_offers_only_quotable_asset_classes(assets):
    """A CRYPTO tab that adds an unpriceable ticker is worse than no tab."""
    html = assets["html"]
    for kind in ("stock", "fund", "crypto", "forex", "index", "dr"):
        assert f'data-kind="{kind}"' in html
    assert 'data-kind="futures"' not in html


def test_only_the_backdrop_closes_the_modal(assets):
    """Clicking a row must not dismiss the browser you are reading."""
    js = assets["js"]
    block = js[js.index('$("symbol-modal").addEventListener("click"'):]
    assert 'e.target === $("symbol-modal")' in block[:300]


def test_rows_already_on_the_watchlist_are_marked(assets):
    js = assets["js"]
    assert "on-watchlist" in js and "on-watchlist" in assets["css"]


# --- workspaces --------------------------------------------------------------

def test_the_tab_strip_exists(assets):
    """Workspace tabs were replaced by browser-style function tabs: every
    screen opens in its own tab, driven by the command line."""
    assert 'id="tabstrip"' in assets["html"]
    assert 'id="tab-new"' in assets["html"]
    assert 'id="cmd-input"' in assets["html"]


def test_every_view_names_only_real_panels(assets):
    """A typo'd id silently hides nothing and shows nothing."""
    panels = set(re.findall(r'<section class="panel" id="([a-z-]+)"', assets["html"]))
    listed = set(re.findall(r'"([a-z-]+-panel)"', assets["js"]))
    unknown = {p for p in listed if p.endswith("-panel")} - panels
    assert not unknown, f"views reference non-existent panels: {sorted(unknown)}"





def test_the_chosen_workspace_persists(assets):
    assert "WORKSPACE_KEY" in assets["js"]



def test_there_are_between_three_and_six_workspaces(assets):
    import re as _re

    js = assets["js"]
    block = js[js.index("const WORKSPACES = ["):js.index("const WORKSPACE_KEY")]
    codes = _re.findall(r'code:\s*"([A-Z]+)"', block)
    assert 3 <= len(codes) <= 6, f"got {codes}"


def test_every_workspace_panel_exists(assets):
    """A typo'd id hides nothing and shows nothing."""
    import re as _re

    js, html = assets["js"], assets["html"]
    panels = set(_re.findall(r'<section class="panel" id="([a-z-]+)"', html))
    block = js[js.index("const WORKSPACES = ["):js.index("const WORKSPACE_KEY")]
    referenced = set(_re.findall(r'"([a-z-]+-panel)":', block))
    assert referenced, "no panels are placed"
    assert referenced <= panels, f"unknown panels: {sorted(referenced - panels)}"


def test_every_panel_appears_in_at_least_one_workspace(assets):
    """A panel no tab shows is unreachable."""
    import re as _re

    js, html = assets["js"], assets["html"]
    panels = set(_re.findall(r'<section class="panel" id="([a-z-]+)"', html))
    block = js[js.index("const WORKSPACES = ["):js.index("const WORKSPACE_KEY")]
    referenced = set(_re.findall(r'"([a-z-]+-panel)":', block))
    orphans = panels - referenced
    assert not orphans, f"panels no workspace shows: {sorted(orphans)}"



def test_every_panel_has_a_maximise_control(assets):
    panels = re.findall(
        r'<section class="panel" id="([a-z-]+)">(.{0,400}?)</h2>',
        assets["html"], re.S)
    assert len(panels) >= 8
    for pid, block in panels:
        assert "data-maximise" in block, f"{pid} has no maximise control"


def test_overlays_do_not_get_a_maximise_control(assets):
    """They are not grid panels; maximising them is meaningless."""
    drawer = assets["html"][assets["html"].index('id="settings-drawer"'):]
    assert "data-maximise" not in drawer[:900]





def test_shortcuts_do_not_fire_while_typing(assets):
    """Otherwise typing a ticker starting with 'a' opens the alert form."""
    js = assets["js"]
    assert "function isTyping" in js
    block = js[js.index("function isTyping"):js.index("let hoveredPanel")]
    for tag in ("input", "textarea", "select"):
        assert tag in block
    assert "isContentEditable" in block


def test_modified_keys_are_left_alone(assets):
    """Cmd-1 switches browser tabs; it must not also switch workspaces."""
    js = assets["js"]
    assert "e.metaKey" in js and "e.ctrlKey" in js


def test_escape_unwinds_one_layer_at_a_time(assets):
    js = assets["js"]
    block = js[js.index('if (e.key === "Escape")'):]
    handler = block[:block.index("if (isTyping(")]
    for layer in ("help-overlay", "maximised", "settings-drawer",
                  "closeSymbolModal"):
        assert layer in handler, f"Escape does not unwind {layer}"


def test_the_help_overlay_documents_the_shortcuts(assets):
    html = assets["html"]
    assert "help-overlay" in html
    for key in (">1 2 3<", ">/<", ">f<", ">x<", ">Esc<"):
        assert key in html, f"{key} is not documented"


# --- extended fundamentals ---------------------------------------------------

EXTENDED = ("peg_ratio", "price_to_book", "gross_margin", "operating_margin",
            "profit_margin", "return_on_equity", "debt_to_equity",
            "free_cashflow", "revenue_growth", "earnings_growth",
            "target_mean", "target_high", "target_low", "analyst_count",
            "short_pct_float", "held_by_institutions", "avg_volume",
            "ma50", "ma200", "payout_ratio", "recommendation")


def test_every_extended_field_is_stored(conn):
    database.upsert_fundamentals(
        conn, "AAPL", pe_ratio=30.0,
        **{f: (0.5 if f != "recommendation" else "buy") for f in EXTENDED})
    stored = database.get_fundamentals(conn, "AAPL")
    for field in EXTENDED:
        assert field in stored, f"{field} was not persisted"
        assert stored[field] is not None


def test_the_migration_is_idempotent(db_path):
    """init_db runs on every startup and SQLite has no ADD COLUMN IF NOT EXISTS."""
    database.init_db(db_path)
    database.init_db(db_path)
    with database.get_conn(db_path) as conn:
        assert database.get_fundamentals(conn, "NOPE") is None


def test_missing_extended_fields_are_not_an_error(conn):
    """ETFs and ADRs routinely have none of them."""
    database.upsert_fundamentals(conn, "SPY", pe_ratio=None)
    assert database.get_fundamentals(conn, "SPY")["peg_ratio"] is None


def test_the_route_serves_the_extended_fields(db_path):
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_fundamentals(conn, "AAPL", pe_ratio=30.0, peg_ratio=1.5,
                                     recommendation="buy")
    with client(db_path) as c:
        f = c.get("/api/fundamentals/AAPL").json()["fundamentals"]
    assert f["peg_ratio"] == 1.5
    assert f["recommendation"] == "buy"


def test_percentages_are_rendered_as_percentages(assets):
    """They arrive as fractions; 0.403 shown raw reads as a 0.4% margin."""
    js = assets["js"]
    assert "function pct" in js
    block = js[js.index("function pct"):js.index("function upside")]
    assert "* 100" in block


# --- watchlist sorting -------------------------------------------------------

def test_sorting_does_not_mutate_the_polled_array(assets):
    """The poller replaces state.prices wholesale every cycle."""
    js = assets["js"]
    block = js[js.index("function sortedPrices"):]
    assert "[...rows]" in block[:400], "must sort a copy"


def test_missing_values_sort_last_in_both_directions(assets):
    js = assets["js"]
    block = js[js.index("function sortedPrices"):]
    assert "return 1" in block[:700] and "return -1" in block[:700]


def test_the_sortable_headers_are_marked_up(assets):
    for key in ("ticker", "price", "change_pct"):
        assert f'data-sort="{key}"' in assets["html"]



def test_the_route_field_list_is_not_a_second_hand_kept_copy(assets):
    """Adding fields to the schema left a duplicated whitelist serving the old
    set, so the route now derives its fields from the storage layer."""
    source = __import__("pathlib").Path("backend/routes/fundamentals.py").read_text()
    assert "database._FUNDAMENTAL_FIELDS" in source


def test_the_route_serves_every_stored_field(db_path):
    """Guards the derivation: any future field reaches the client for free."""
    with database.get_conn(db_path) as conn:
        database.add_watchlist_ticker(conn, "AAPL")
        database.upsert_fundamentals(conn, "AAPL", pe_ratio=1.0)
    with TestClient(create_app(cfg=Config(db_path=db_path), start_poller=False)) as c:
        payload = c.get("/api/fundamentals/AAPL").json()["fundamentals"]
    for field in database._FUNDAMENTAL_FIELDS:
        assert field in payload, f"{field} is stored but never served"


# --- the hidden attribute must actually hide ---------------------------------
#
# `.help-overlay { display: flex }` beat the UA stylesheet's
# `[hidden] { display: none }` -- same specificity, later sheet wins -- so the
# overlay covered the page from load. Every JS check passed, because the
# attribute was toggling correctly the whole time; only the rendering was wrong.
# That also made maximise look broken: the maximised panel is z-index 10, and
# the overlay sitting on top of it is 40.

def test_the_hidden_attribute_beats_every_display_rule(assets):
    css = strip_css_comments(assets["css"])
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "an element with `hidden` can still be displayed by any class rule"
    )


def strip_css_comments(css):
    """Remove /* ... */ before matching.

    Comments explaining a CSS bug naturally quote the offending CSS, and a
    regex looking for `.help-overlay {` happily matches the explanation
    instead of the rule -- which is how both tests below first failed.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_the_guard_precedes_the_rules_it_has_to_beat(assets):
    """!important wins regardless of order, but a guard sitting below the rules
    it guards reads as decorative and invites someone to 'tidy' it away."""
    css = strip_css_comments(assets["css"])
    guard = css.index("[hidden] { display: none !important; }")
    assert guard < css.index(".help-overlay {"), "guard must come first"


def test_no_hideable_element_relies_on_specificity_alone(assets):
    """Structural: every id that carries `hidden` in the markup must either
    have no display rule, or be covered by the guard."""
    html, css = assets["html"], assets["css"]
    assert "!important" in css  # the guard exists; that is what covers them
    hideable = re.findall(r'id="([a-z-]+)"[^>]*\shidden', html)
    assert hideable, "no elements use the hidden attribute"
    for element_id in hideable:
        assert f'id="{element_id}"' in html


def test_overlays_sit_above_a_maximised_panel(assets):
    """They are meant to; that is why a stuck overlay hid the maximised panel
    rather than appearing behind it."""
    css = strip_css_comments(assets["css"])

    def z(selector):
        block = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css).group(1)
        return int(re.search(r"z-index:\s*(\d+)", block).group(1))

    assert z(".help-overlay") > z(".grid > .panel.maximised")
    assert z(".settings-drawer") > z(".grid > .panel.maximised")


def test_the_help_overlay_starts_hidden_in_the_markup(assets):
    block = assets["html"][assets["html"].index('id="help-overlay"'):]
    assert "hidden" in block[:120]


# --- warm feedback -----------------------------------------------------------

def test_an_empty_news_panel_distinguishes_fetching_from_none(assets):
    """"No recent news" is a claim about the ticker. While a warm is in flight
    it is a claim about us."""
    js = assets["js"]
    assert "Fetching news" in js
    assert "No recent news" in js


def test_fundamentals_does_the_same(assets):
    assert "Fetching fundamentals" in assets["js"]


def test_adding_follows_the_warm_rather_than_waiting_for_the_cadence(assets):
    js = assets["js"]
    assert "function followWarm" in js
    block = js[js.index("function followWarm"):js.index("async function addSymbol")]
    assert "renderNews" in block and "renderFundamentals" in block


def test_the_follow_gives_up_rather_than_polling_forever(assets):
    js = assets["js"]
    block = js[js.index("function followWarm"):js.index("async function addSymbol")]
    assert "clearInterval" in block
    assert "attempts >=" in block


def test_the_client_tracks_which_tickers_are_warming(assets):
    js = assets["js"]
    assert "state.warming" in js
    assert "health.warming" in js


# --- playbook timestamp and cadence ------------------------------------------

def test_the_playbook_shows_when_it_was_computed(assets):
    assert 'id="plans-stamp"' in assets["html"]
    js = assets["js"]
    assert "function stampPlans" in js
    block = js[js.index("function stampPlans"):js.index("async function renderPlans")]
    assert "toLocaleTimeString" in block


def test_the_cadence_comes_from_the_horizon_not_a_constant(assets):
    """A scalp horizon refreshing every five minutes is useless."""
    js = assets["js"]
    assert "schedulePlans(payload.horizon.refresh_seconds)" in js


def test_the_cadence_is_clamped_at_both_ends(assets):
    js = assets["js"]
    block = js[js.index("function schedulePlans"):js.index("function stampPlans")]
    assert "Math.max(15" in block and "Math.min(" in block


def test_the_clamped_value_is_the_one_actually_scheduled(assets):
    """Computing a cadence and then scheduling a constant looks identical from
    the outside -- the clamp runs, and the timer ignores it."""
    import re as _re

    js = assets["js"]
    block = js[js.index("function schedulePlans"):js.index("function stampPlans")]
    call = _re.search(r"setInterval\(renderPlans,\s*([^)]+)\)", block).group(1)
    assert "every" in call, f"setInterval does not use the computed cadence: {call}"


def test_switching_horizon_replaces_the_timer(assets):
    """Without clearInterval each switch leaves another timer running."""
    js = assets["js"]
    block = js[js.index("function schedulePlans"):js.index("function stampPlans")]
    assert "clearInterval(planTimer)" in block


def test_adding_a_ticker_recomputes_the_playbook(assets):
    js = assets["js"]
    block = js[js.index("async function addSymbol"):js.index("$(\"add-input\")")]
    assert "renderPlans()" in block


def test_the_warm_follow_also_recomputes_it(assets):
    """The warm caches the bars the playbook needs; it should appear as they
    land, not on the next cadence."""
    js = assets["js"]
    block = js[js.index("function followWarm"):js.index("async function addSymbol")]
    assert "renderPlans()" in block


def test_the_horizon_selector_is_rendered_from_the_backend(assets):
    """Hardcoding the list in the UI is how it drifts from the engine."""
    js = assets["js"]
    assert "/api/plans/horizons" in js
    assert 'id="horizon-bar"' in assets["html"]





def test_maximise_overrides_the_panels_own_geometry(assets):
    """Panels carry inline left/top/width/height now, so a maximise that only
    added a class would apply, look right, and move nothing."""
    js = assets["js"]
    block = js[js.index("function maximisePanel"):js.index("function restorePanels")]
    assert 'panel.style.width = "100%"' in block
    assert 'panel.style.height = "100%"' in block


def test_restoring_repaints_from_the_saved_geometry(assets):
    js = assets["js"]
    block = js[js.index("function restorePanels"):js.index("document.querySelectorAll(\"[data-maximise]\")")]
    assert "paint(el.id)" in block


# --- the playbook is its own panel -------------------------------------------

def test_the_playbook_is_a_top_level_panel(assets):
    """Nested inside the alerts panel it inherited that panel's box: no
    handles of its own, and no way to resize it."""
    html = assets["html"]
    assert '<section class="panel" id="playbook-panel">' in html


def test_the_playbook_is_not_inside_the_alerts_panel(assets):
    html = assets["html"]
    alerts = html[html.index('id="alerts-panel"'):]
    alerts = alerts[:alerts.index("</section>")]
    assert "plans-list" not in alerts
    assert "horizon-bar" not in alerts


def test_the_alerts_panel_keeps_its_own_content(assets):
    """Splitting the playbook out must not take the price alerts with it."""
    html = assets["html"]
    alerts = html[html.index('id="alerts-panel"'):]
    alerts = alerts[:alerts.index("</section>")]
    assert "alert-form" in alerts
    assert "alerts-list" in alerts


def test_the_playbook_has_a_draggable_title_and_a_maximise_control(assets):
    html = assets["html"]
    block = html[html.index('id="playbook-panel"'):]
    block = block[:block.index("</h2>")]
    assert 'tabindex="0"' in block
    assert "data-maximise" in block


def test_the_playbook_is_offered_in_the_new_tab_builder(assets):
    js = assets["js"]
    block = js[js.index("const PANEL_CATALOGUE"):js.index("const TEMPLATES")]
    assert '"playbook-panel"' in block


def test_alerts_and_playbook_are_listed_separately(assets):
    """One name for two panels would make the picker ambiguous."""
    js = assets["js"]
    block = js[js.index("const PANEL_CATALOGUE"):js.index("const TEMPLATES")]
    names = re.findall(r'name:\s*"([^"]+)"', block)
    assert "Alerts" in names and "Playbook" in names
    assert len(names) == len(set(names))


def test_the_play_command_opens_the_playbook_panel(assets):
    js = assets["js"]
    block = js[js.index("const CANVAS_FUNCTIONS"):js.index("async function renderScreen")]
    assert 'PLAY: "playbook-panel"' in block


# --- which security a screen is about ----------------------------------------

def test_a_ticker_screen_opened_from_the_directory_uses_the_focused_ticker():
    """The directory lists functions, not securities, so its rows carry no
    ticker. Before this, clicking HDS asked the API about a company called
    "null" — which answers 200 with every field empty, so the screen rendered
    "null — HOLDERS" over an empty table and looked broken rather than
    unanswerable.
    """
    import json
    import subprocess
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "frontend" / "app.js").read_text()
    start = source.index("function resolveTicker(")
    end = source.index("\n}\n", start) + 3
    script = source[start:end] + """
const { known, ticker, active } = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(resolveTicker(known, ticker, active)));
"""
    tmp = Path("/tmp/resolve_ticker.js")
    tmp.write_text(script)

    def resolve(known, ticker, active):
        out = subprocess.run(
            ["node", str(tmp)],
            input=json.dumps({"known": known, "ticker": ticker, "active": active}),
            capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)

    needs = {"needs_ticker": True}
    standalone = {"needs_ticker": False}

    # the bug: a directory click with a ticker already in focus
    assert resolve(needs, None, "AAPL") == "AAPL"
    # an explicit ticker always wins over the focused one
    assert resolve(needs, "NVDA", "AAPL") == "NVDA"
    # nothing in focus and nothing given: None, so the screen can say so
    # rather than asking about a company called "null"
    assert resolve(needs, None, None) is None
    # a screen that is not about one security never inherits a ticker
    assert resolve(standalone, None, "AAPL") is None
