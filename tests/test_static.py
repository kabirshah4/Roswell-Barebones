from fastapi.testclient import TestClient

from backend.config import Config
from backend.main import create_app


def make_client(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    return TestClient(app)


def test_root_serves_html(db_path):
    with make_client(db_path) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "ROSWELL" in r.text or "Roswell" in r.text


def test_static_assets_are_served(db_path):
    with make_client(db_path) as c:
        assert c.get("/static/app.js").status_code == 200
        assert c.get("/static/style.css").status_code == 200


def test_api_and_root_routes_all_stay_reachable_alongside_static_mount(db_path):
    """Regression guard, not an ordering guard.

    The static mount is registered at the "/static" prefix, which can never
    collide with "/api/*" or "/" regardless of registration order, so this
    test cannot detect shadowing by construction. The real ordering rule —
    `app.mount("/static", ...)` must come after both `include_router` calls
    — is documented and enforced at the point of registration in
    `backend/main.py`. What this test verifies is that the API, the root
    HTML route, and the static mount all coexist and are simultaneously
    reachable on one app instance.
    """
    with make_client(db_path) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        assert c.get("/api/watchlist").json() == {"tickers": []}
        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200


def test_index_contains_news_and_fundamentals_panels(db_path):
    with make_client(db_path) as c:
        html = c.get("/").text
    assert "news-panel" in html
    assert "fundamentals-panel" in html


def test_index_contains_screener_and_macro_panels(db_path):
    with make_client(db_path) as c:
        html = c.get("/").text
    assert "screener-panel" in html
    assert "macro-panel" in html


def test_index_contains_chat_panel(db_path):
    with make_client(db_path) as c:
        assert "chat-panel" in c.get("/").text


def test_every_panel_is_placed_by_a_workspace(db_path):
    """Panels are positioned absolutely now, so the old "has a grid-row" check
    no longer applies -- but a panel no workspace places is still invisible."""
    import re

    with make_client(db_path) as c:
        html = c.get("/").text
        js = c.get("/static/app.js").text

    panels = set(re.findall(r'<section class="panel" id="([a-z-]+)"', html))
    block = js[js.index("const WORKSPACES = ["):js.index("const WORKSPACE_KEY")]
    placed = set(re.findall(r'"([a-z-]+-panel)":\s*\[', block))
    assert panels, "no panels found"
    assert not (panels - placed), f"unplaced panels: {sorted(panels - placed)}"


def test_chart_container_has_a_minimum_height(db_path):
    """The chart is an iframe; without a min-height its row can collapse to 0."""
    import re

    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    block = re.search(r"\.chart-container\s*\{([^}]*)\}", css)
    assert block, ".chart-container rule missing"
    assert "min-height" in block.group(1)


def test_palette_carries_only_two_hues(db_path):
    """Green and red are the only colours; everything else must be neutral.

    Colour in this interface means direction. A third hue would make it
    decoration, and a reader could no longer trust that colour = up or down.
    """
    import re

    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    allowed_hues = {"#26a65b", "#e0483e"}   # up, down
    offenders = []
    for hexcode in set(re.findall(r"#[0-9a-fA-F]{6}", css)):
        low = hexcode.lower()
        if low in allowed_hues:
            continue
        r, g, b = (int(low[i:i + 2], 16) for i in (1, 3, 5))
        if max(r, g, b) - min(r, g, b) >= 12:   # visibly saturated
            offenders.append(hexcode)

    assert not offenders, f"non-neutral colours outside green/red: {sorted(offenders)}"


def test_borders_are_hairlines_not_full_pixels(db_path):
    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    assert "1px solid" not in css, "borders should use var(--hairline), not 1px"
    assert "--hairline" in css


def test_desktop_layout_is_locked_to_one_viewport(db_path):
    """The app should fill the screen exactly, with panels scrolling internally.

    A `height: auto` grid combined with a tall chart floor let content push the
    page past the viewport, forcing a full-page scroll.
    """
    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    assert "height: calc(100vh - var(--topbar))" in css
    assert "min-height: calc(100vh" not in css, "min-height lets the grid grow past the screen"


def test_narrow_layout_can_still_scroll(db_path):
    """Locking the page at one viewport must not trap the stacked mobile layout."""
    import re

    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    narrow = re.search(r"@media \(max-width: 1100px\) \{(.*?)\n\}", css, re.S)
    assert narrow, "narrow breakpoint missing"
    assert "overflow: auto" in narrow.group(1)


def test_the_canvas_establishes_a_positioning_context(db_path):
    """Absolutely positioned panels fall back to the viewport without it,
    escaping the canvas entirely."""
    import re

    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    block = re.search(r"\n\.grid \{([^}]*)\}", css).group(1)
    assert "position: relative" in block
    assert "overflow: hidden" in block


def test_the_topbar_height_matches_the_variable_the_canvas_subtracts(db_path):
    """The canvas is sized as 100vh minus --topbar and --tabstrip. If the bar
    renders taller than --topbar claims, the canvas overflows the window by
    exactly that difference — which is what a taller ASCII wordmark caused."""
    import re

    with make_client(db_path) as c:
        css = c.get("/static/style.css").text

    block = re.search(r"\n\.topbar \{([^}]*)\}", css).group(1)
    assert "height: var(--topbar)" in block
    assert "box-sizing: border-box" in block


def test_the_wordmark_rows_are_the_same_width(db_path):
    """Ragged rows shear the art."""
    import re

    with make_client(db_path) as c:
        html = c.get("/").text

    art = re.search(r'aria-hidden="true"\s*>(.*?)</span>', html, re.S).group(1)
    rows = art.split("\n")
    assert len(rows) == 3
    assert len({len(r) for r in rows}) == 1, [len(r) for r in rows]


def test_the_wordmark_has_a_text_label(db_path):
    """Box-drawing glyphs read as noise to a screen reader."""
    with make_client(db_path) as c:
        html = c.get("/").text
    assert 'aria-label="Roswell"' in html
    assert 'aria-hidden="true"' in html
