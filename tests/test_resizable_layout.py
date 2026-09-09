"""The panel canvas: absolute geometry, drag, resize, snap.

The old model shared four grid tracks between all nine panels. Dragging one
edge moved several panels at once, and its splitters drew lines at fixed
gutters straight through any panel that spanned one. Panels are now positioned
individually on a 24x24 snap lattice.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.main import create_app


@pytest.fixture
def assets(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        return {
            "html": c.get("/").text,
            "css": c.get("/static/style.css").text,
            "js": c.get("/static/app.js").text,
        }


def arranger(js):
    return js[js.index("// --- Panel arranging"):js.index("// --- Screener presets")]


def workspaces(js):
    return js[js.index("const WORKSPACES = ["):js.index("const WORKSPACE_KEY")]


def layouts(js):
    """Every workspace layout, as {panel: [x, y, w, h]}."""
    out = []
    for block in re.findall(r"layout:\s*\{(.*?)\n    \}", workspaces(js), re.S):
        out.append({
            panel: json.loads(f"[{raw}]")
            for panel, raw in re.findall(r'"([a-z-]+-panel)":\s*\[([\d, ]+)\]', block)
        })
    return out


# --- the canvas --------------------------------------------------------------

def test_the_canvas_positions_panels_absolutely(assets):
    css = assets["css"]
    block = re.search(r"\n\.grid \{([^}]*)\}", css).group(1)
    assert "position: relative" in block
    panel = re.search(r"\n\.panel \{([^}]*)\}", css).group(1)
    assert "position: absolute" in panel


def test_the_splitters_are_gone(assets):
    """They drew a line at a fixed gutter regardless of what was there."""
    assert "splitter" not in assets["html"]
    assert 'class="splitter' not in assets["css"]


def test_geometry_is_stored_in_snap_units_not_pixels(assets):
    """A pixel layout breaks the moment the window changes size."""
    js = arranger(assets["js"])
    assert "const SNAP_X = 24" in js and "const SNAP_Y = 24" in js
    block = js[js.index("function paint(id)"):js.index("function paintAll")]
    assert "SNAP_X" in block and "SNAP_Y" in block
    assert "%`" in block, "geometry must paint as percentages, not pixels"


def test_a_resize_only_repaints(assets):
    js = arranger(assets["js"])
    assert re.search(r'addEventListener\("resize", paintAll\)', js)


# --- dragging and resizing ---------------------------------------------------

def test_a_panel_is_dragged_by_its_title(assets):
    js = arranger(assets["js"])
    assert '.closest(".panel-title")' in js


def test_controls_in_the_title_bar_still_work(assets):
    """Otherwise the maximise button starts a drag instead of maximising."""
    js = arranger(assets["js"])
    assert 'closest("button, a, input, select")' in js


def test_every_edge_and_corner_resizes(assets):
    js = arranger(assets["js"])
    handles = re.search(r'\[([^\]]*)\]\.forEach\(\(edge\)', js).group(1)
    for edge in ("n", "s", "w", "e", "nw", "ne", "sw", "se"):
        assert f'"{edge}"' in handles, f"no {edge} handle"


def test_corner_handles_come_after_edges(assets):
    """A later sibling wins the hit test where they overlap; corners first
    would make them unreachable."""
    js = arranger(assets["js"])
    handles = re.search(r'\[([^\]]*)\]\.forEach\(\(edge\)', js).group(1)
    order = [h.strip(' "') for h in handles.split(",")]
    assert order.index("nw") > order.index("n")
    assert order.index("se") > order.index("e")


def test_dragging_the_left_edge_moves_x_and_w_together(assets):
    """Clamping width alone lets the panel creep sideways at minimum size."""
    js = arranger(assets["js"])
    block = js[js.index('if (edge.includes("w"))'):js.index('if (edge.includes("n"))')]
    assert "g.x =" in block and "g.w =" in block


def test_geometry_is_clamped_to_the_canvas(assets):
    js = arranger(assets["js"])
    block = js[js.index("function clampGeometry"):js.index("function paint(")]
    assert "MIN_W" in block and "MIN_H" in block
    assert "SNAP_X - g.w" in block, "a panel could be dragged off the right edge"
    assert "SNAP_Y - g.h" in block


def test_a_minimum_size_is_enforced(assets):
    js = arranger(assets["js"])
    assert re.search(r"const MIN_W = [1-9]", js)
    assert re.search(r"const MIN_H = [1-9]", js)


def test_the_drag_snaps_to_the_lattice(assets):
    """Rounding is what makes one panel's edge land where its neighbour's is."""
    js = arranger(assets["js"])
    assert "Math.round((e.clientX - startX) * scale.x)" in js


def test_the_iframe_cannot_swallow_a_drag(assets):
    """The chart iframe captures the pointer the moment the cursor crosses it."""
    assert "body.arranging iframe" in assets["css"]
    assert "pointer-events: none" in assets["css"]


def test_panels_are_keyboard_arrangeable(assets):
    js = arranger(assets["js"])
    assert 'grid.addEventListener("keydown"' in js
    assert "e.altKey" in js, "no way to resize without a pointer"
    assert 'tabindex="0"' in assets["html"]


# --- persistence -------------------------------------------------------------

def test_the_layout_is_saved_per_workspace(assets):
    """A layout dragged for RESEARCH should not follow you to TRADE, where
    different panels occupy that space."""
    js = arranger(assets["js"])
    block = js[js.index("function layoutStorageKey"):js.index("function saveGeometry")]
    assert "WORKSPACES[activeWorkspace].key" in block


def test_storage_failures_are_tolerated(assets):
    """localStorage throws outright in some private-browsing modes."""
    js = arranger(assets["js"])
    for name in ("function saveGeometry", "function readGeometry"):
        block = js[js.index(name):js.index(name) + 400]
        assert "try" in block and "catch" in block


def test_reset_clears_only_this_workspace(assets):
    js = arranger(assets["js"])
    block = js[js.index('$("reset-layout")'):]
    assert "layoutStorageKey()" in block[:300]


def test_a_saved_panel_the_tab_hides_is_not_resurrected(assets):
    js = assets["js"]
    block = js[js.index("function applyWorkspace"):js.index("// --- command-line")]
    assert "saved[id]" in block
    assert "el.hidden = !place" in block


# --- the default layouts -----------------------------------------------------

def test_every_workspace_tiles_the_canvas_completely(assets):
    """A gap is wasted screen and an overlap hides a panel. Computed cell by
    cell rather than eyeballed."""
    for i, layout in enumerate(layouts(assets["js"])):
        covered = {}
        for panel, (x, y, w, h) in layout.items():
            for cx in range(x, x + w):
                for cy in range(y, y + h):
                    clash = covered.get((cx, cy))
                    assert clash is None, (
                        f"workspace {i}: {panel} and {clash} overlap at ({cx},{cy})"
                    )
                    covered[(cx, cy)] = panel
        missing = [
            (x, y) for x in range(24) for y in range(24) if (x, y) not in covered
        ]
        assert not missing, (
            f"workspace {i} leaves {len(missing)} cells empty, "
            f"first at {missing[0]}"
        )


def test_no_layout_runs_off_the_lattice(assets):
    for layout in layouts(assets["js"]):
        for panel, (x, y, w, h) in layout.items():
            assert 0 <= x and x + w <= 24, f"{panel} runs off horizontally"
            assert 0 <= y and y + h <= 24, f"{panel} runs off vertically"


def test_every_default_panel_meets_the_minimum_size(assets):
    for layout in layouts(assets["js"]):
        for panel, (_, _, w, h) in layout.items():
            assert w >= 3 and h >= 3, f"{panel} ships below the minimum size"


def test_every_panel_appears_in_at_least_one_workspace(assets):
    """A panel no tab shows is unreachable."""
    panels = set(re.findall(r'<section class="panel" id="([a-z-]+)"', assets["html"]))
    shown = set()
    for layout in layouts(assets["js"]):
        shown |= set(layout)
    assert not (panels - shown), f"unreachable panels: {sorted(panels - shown)}"


def test_the_snap_guide_only_shows_while_arranging(assets):
    css = assets["css"]
    assert ".grid.arranging .snap-guide" in css
    block = re.search(r"\n\.snap-guide \{([^}]*)\}", css).group(1)
    assert "opacity: 0" in block
    assert "pointer-events: none" in block


# --- stale saved layouts -----------------------------------------------------
#
# A layout saved by an earlier build read back as NaN width and height. The
# browser silently ignores those, so every panel fell back to its content size
# and the app looked broken rather than merely stale. None of the tests above
# would have caught it: they all check a fresh browser.

def test_stored_geometry_is_validated_before_use(assets):
    js = arranger(assets["js"])
    assert "function isUsableGeometry" in js
    block = js[js.index("function isUsableGeometry"):js.index("function saveGeometry")]
    assert "Number.isFinite" in block, "a non-numeric entry would become NaN"
    assert "MIN_W" in block and "MIN_H" in block


def test_the_default_is_used_when_a_stored_entry_is_unusable(assets):
    js = assets["js"]
    block = js[js.index("function applyWorkspace"):js.index("// --- command-line")]
    assert "isUsableGeometry(stored)" in block
    assert "{ x, y, w, h }" in block, "no fallback to the tab's default"


def test_clamping_coerces_before_it_compares(assets):
    """Math.min(undefined, 24) is NaN, and it propagates all the way out to an
    inline style the browser then ignores."""
    js = arranger(assets["js"])
    block = js[js.index("function clampGeometry"):js.index("function paint(")]
    assert "Number.isFinite" in block


def test_the_storage_key_is_versioned(assets):
    """Bumping it retires an earlier build's data outright, rather than hoping
    validation catches every variant."""
    js = arranger(assets["js"])
    # A non-empty value, not merely the identifier: an empty version string
    # produces the same key the earlier build wrote.
    assert re.search(r'const GEOMETRY_VERSION = "v\d+"', js)
    block = js[js.index("function layoutStorageKey"):js.index("function isUsableGeometry")]
    assert "GEOMETRY_VERSION" in block


def test_legacy_keys_are_swept_on_load(assets):
    js = arranger(assets["js"])
    assert "discardLegacyGeometry" in js
    block = js[js.index("function discardLegacyGeometry"):js.index("function readGeometry")]
    assert "diy-terminal-layout" in block, "the retired track layout is not cleared"
    # Both sweeps must actually remove: checking for one occurrence passes
    # while the other quietly does nothing.
    assert block.count("localStorage.removeItem(k)") == 2


def test_the_sweep_cannot_throw(assets):
    """localStorage access throws outright in some private-browsing modes, and
    this runs before anything else."""
    js = arranger(assets["js"])
    block = js[js.index("function discardLegacyGeometry"):js.index("function readGeometry")]
    assert "try" in block and "catch" in block


def test_paint_refuses_a_non_finite_geometry(assets):
    """A NaN percentage is not an error the browser reports: it drops the
    declaration, and an absolutely positioned panel with no width falls back
    to its content size. That reached a user once."""
    js = arranger(assets["js"])
    block = js[js.index("function paint(id)"):js.index("function paintAll")]
    assert "Number.isFinite" in block
    assert "return" in block


def test_the_page_serves_content_hashed_asset_urls(db_path):
    """StaticFiles sends an ETag but no Cache-Control, so a browser applies
    heuristic freshness and can serve a stale app.js without revalidating —
    which is how a shipped fix failed to reach the browser and the bug looked
    unfixed."""
    import re as _re

    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        r = c.get("/")
    assert _re.search(r"/static/app\.js\?v=[0-9a-f]{6,}", r.text)
    assert _re.search(r"/static/style\.css\?v=[0-9a-f]{6,}", r.text)


def test_the_html_itself_is_never_stored(db_path):
    """It carries the hashes; a cached copy would point at the old assets."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        r = c.get("/")
    assert "no-store" in r.headers.get("cache-control", "")


def test_the_hash_tracks_the_file_contents(db_path, tmp_path):
    """A fixed version string would defeat the whole point."""
    from backend.main import _asset_version

    a = tmp_path / "a.js"
    a.write_text("one", encoding="utf-8")
    first = _asset_version(a)
    a.write_text("two", encoding="utf-8")
    assert _asset_version(a) != first


def test_a_missing_asset_does_not_break_the_page(tmp_path):
    from backend.main import _asset_version

    assert _asset_version(tmp_path / "absent.js")
