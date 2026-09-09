"""Building your own tab: template, panels, name.

The five built-in tabs cover the jobs that could be anticipated. This covers
the ones that could not.
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
        return {"html": c.get("/").text, "css": c.get("/static/style.css").text,
                "js": c.get("/static/app.js").text}


def templates(js):
    block = js[js.index("const TEMPLATES = ["):js.index("const CUSTOM_TABS_KEY")]
    out = []
    for name, raw in re.findall(
        r'name:\s*"([^"]+)".*?slots:\s*\[(.*?)\]\s*\}', block, re.S
    ):
        slots = [json.loads(f"[{s}]") for s in re.findall(r"\[([\d, ]+)\]", raw)]
        out.append((name, slots))
    return out


def catalogue(js):
    block = js[js.index("const PANEL_CATALOGUE = ["):js.index("const TEMPLATES")]
    return re.findall(r'id:\s*"([a-z-]+)"', block)


# --- the builder exists ------------------------------------------------------

def test_the_new_tab_button_opens_the_builder(assets):
    assert 'id="newtab-modal"' in assets["html"]
    assert '$("tab-new").addEventListener("click", () => openNewTab())' in assets["js"]


def test_the_builder_asks_for_a_name_template_and_panels(assets):
    html = assets["html"]
    assert 'id="newtab-name"' in html
    assert 'id="template-grid"' in html
    assert 'id="panel-picker"' in html


# --- the panel list ----------------------------------------------------------

def test_every_real_panel_is_offered(assets):
    """"Tell me the list of everything" — a panel missing from the picker is
    a panel you cannot put on a custom tab."""
    panels = set(re.findall(r'<section class="panel" id="([a-z-]+)"', assets["html"]))
    offered = set(catalogue(assets["js"]))
    assert not (panels - offered), f"not offered: {sorted(panels - offered)}"


def test_the_picker_offers_nothing_that_does_not_exist(assets):
    panels = set(re.findall(r'<section class="panel" id="([a-z-]+)"', assets["html"]))
    offered = set(catalogue(assets["js"]))
    assert not (offered - panels), f"phantom panels: {sorted(offered - panels)}"


def test_each_entry_is_labelled_in_plain_words(assets):
    """"chart-panel" is an id, not a description."""
    block = assets["js"]
    block = block[block.index("const PANEL_CATALOGUE"):block.index("const TEMPLATES")]
    entries = re.findall(r'name:\s*"([^"]+)",\s*what:\s*"([^"]+)"', block)
    assert len(entries) == len(catalogue(assets["js"]))
    for name, what in entries:
        assert name and what


# --- the templates -----------------------------------------------------------

def test_there_are_several_templates(assets):
    assert len(templates(assets["js"])) >= 4


def test_every_template_tiles_the_lattice_completely(assets):
    """A custom tab must not ship with the dead space the built-ins avoid."""
    for name, slots in templates(assets["js"]):
        covered = {}
        for x, y, w, h in slots:
            for cx in range(x, x + w):
                for cy in range(y, y + h):
                    assert (cx, cy) not in covered, f"{name} overlaps at ({cx},{cy})"
                    covered[(cx, cy)] = True
        missing = [
            (x, y) for x in range(24) for y in range(24) if (x, y) not in covered
        ]
        assert not missing, f"{name} leaves {len(missing)} cells empty"


def test_no_template_slot_runs_off_the_lattice(assets):
    for name, slots in templates(assets["js"]):
        for x, y, w, h in slots:
            assert x + w <= 24 and y + h <= 24, f"{name} runs off the grid"


def test_every_slot_meets_the_minimum_panel_size(assets):
    for name, slots in templates(assets["js"]):
        for _, _, w, h in slots:
            assert w >= 3 and h >= 3, f"{name} has an unusably small slot"


def test_templates_show_a_preview_not_just_a_name(assets):
    """"MAIN + 2 RAIL" means nothing until you have used it once."""
    assert "template-preview" in assets["js"]
    assert ".template-preview" in assets["css"]


# --- creating and deleting ---------------------------------------------------

def test_a_custom_tab_is_stored_under_a_versioned_key(assets):
    assert re.search(r'const CUSTOM_TABS_KEY = "[^"]*\.v\d+"', assets["js"])


def test_stored_tabs_are_validated_before_use(assets):
    """A tab naming a panel that no longer exists renders empty."""
    js = assets["js"]
    block = js[js.index("function readCustomTabs"):js.index("function saveCustomTabs")]
    assert "PANEL_CATALOGUE.some" in block
    assert "try" in block and "catch" in block


def test_panels_beyond_the_slot_count_cannot_be_added(assets):
    js = assets["js"]
    block = js[js.index('$("panel-picker").addEventListener'):
               js.index('$("newtab-create")')]
    assert "draftTemplate.slots.length" in block


def test_changing_template_drops_panels_that_no_longer_fit(assets):
    """Keeping a fifth panel on a four-slot template would silently lose it."""
    js = assets["js"]
    block = js[js.index('$("template-grid").addEventListener'):
               js.index('$("panel-picker").addEventListener')]
    assert "slice(0, draftTemplate.slots.length)" in block


def test_creating_with_no_panels_is_refused(assets):
    js = assets["js"]
    block = js[js.index('$("newtab-create")'):js.index("function deleteCustomTab")]
    assert "draftPanels.length === 0" in block


def test_a_custom_tab_can_be_deleted(assets):
    assert "function deleteCustomTab" in assets["js"]
    assert "data-delete-tab" in assets["js"]


def test_deleting_also_clears_its_saved_arrangement(assets):
    """Otherwise a new tab reusing the key inherits a stranger's layout."""
    js = assets["js"]
    block = js[js.index("function deleteCustomTab"):]
    assert "removeItem" in block[:400]


def test_builtin_tabs_have_no_delete_control(assets):
    """The five built-ins are the app's structure, not user data."""
    js = assets["js"]
    block = js[js.index("function renderTabs"):js.index("function trackKey")] \
        if "function trackKey" in js else js[js.index("function renderTabs"):
                                             js.index("function applyWorkspace")]
    assert "w.custom" in block, "the delete control is not gated on custom tabs"


def test_custom_tabs_are_appended_after_the_builtins(assets):
    js = assets["js"]
    assert "BUILTIN_WORKSPACE_COUNT" in js
    block = js[js.index("function refreshWorkspaces"):js.index("let draftTemplate")]
    assert "WORKSPACES.length = BUILTIN_WORKSPACE_COUNT" in block


def test_a_custom_tab_becomes_a_normal_workspace(assets):
    """So it drags, resizes and saves its arrangement like any other."""
    js = assets["js"]
    block = js[js.index("function customToWorkspace"):js.index("function refreshWorkspaces")]
    assert "layout" in block and "template.slots" in block


# --- the expanded template set -----------------------------------------------

def test_there_are_plenty_of_templates(assets):
    assert len(templates(assets["js"])) >= 15


def test_template_keys_are_unique(assets):
    js = assets["js"]
    block = js[js.index("const TEMPLATES = ["):js.index("const CUSTOM_DIVISORS")]
    keys = re.findall(r'key:\s*"([a-z0-9-]+)"', block)
    assert len(keys) == len(set(keys)), "duplicate template keys"


def test_every_template_has_a_preview_grid_matching_its_slots(assets):
    """A preview that does not match the arrangement is worse than none."""
    js = assets["js"]
    block = js[js.index("const TEMPLATES = ["):js.index("const CUSTOM_DIVISORS")]
    for cols, rows, raw in re.findall(
        r'cols:\s*"([^"]+)",\s*rows:\s*"([^"]+)",\s*slots:\s*\[(.*?)\]\s*\}',
        block, re.S
    ):
        cells = len(re.findall(r"\[([\d, ]+)\]", raw))
        tracks = len(cols.split()) * len(rows.split())
        assert cells <= tracks, f"preview has {tracks} cells for {cells} slots"


# --- the custom grid ---------------------------------------------------------

def test_a_custom_grid_can_be_generated(assets):
    assert "function buildCustomTemplate" in assets["js"]
    assert "data-cols=" in assets["js"] and "data-rows=" in assets["js"]


def test_only_divisors_of_the_lattice_are_offered(assets):
    """24 divides by these exactly, so every generated slot lands on whole
    lattice units with nothing left over — which is why the lattice is 24."""
    js = assets["js"]
    divisors = re.search(r"const CUSTOM_DIVISORS = \[([\d, ]+)\]", js).group(1)
    for n in json.loads(f"[{divisors}]"):
        assert 24 % n == 0, f"{n} does not divide the lattice"


def test_a_generated_grid_tiles_completely(assets, tmp_path):
    """Run the real generator, not a reimplementation of it."""
    import subprocess

    js = assets["js"]
    src = "\n".join([
        "const SNAP_X = 24, SNAP_Y = 24;",
        js[js.index("const CUSTOM_DIVISORS"):js.index("let draftTemplate")],
        """
const out = [];
for (const c of CUSTOM_DIVISORS) for (const r of CUSTOM_DIVISORS) {
  out.push({c, r, slots: buildCustomTemplate(c, r).slots});
}
process.stdout.write(JSON.stringify(out));
""",
    ])
    script = tmp_path / "grid.js"
    script.write_text(src)
    grids = json.loads(
        subprocess.run(["node", str(script)], capture_output=True, text=True,
                       check=True).stdout
    )
    assert len(grids) == 25
    for g in grids:
        covered = set()
        for x, y, w, h in g["slots"]:
            for cx in range(x, x + w):
                for cy in range(y, y + h):
                    assert (cx, cy) not in covered, f"{g['c']}x{g['r']} overlaps"
                    covered.add((cx, cy))
        assert len(covered) == 24 * 24, f"{g['c']}x{g['r']} leaves gaps"
        assert len(g["slots"]) == g["c"] * g["r"]


def test_an_out_of_range_grid_falls_back(assets, tmp_path):
    """A stored tab could name a size the generator no longer offers."""
    import subprocess

    js = assets["js"]
    src = "\n".join([
        "const SNAP_X = 24, SNAP_Y = 24;",
        js[js.index("const CUSTOM_DIVISORS"):js.index("let draftTemplate")],
        "process.stdout.write(JSON.stringify(buildCustomTemplate(5, 99).slots));",
    ])
    script = tmp_path / "fallback.js"
    script.write_text(src)
    slots = json.loads(
        subprocess.run(["node", str(script)], capture_output=True, text=True,
                       check=True).stdout
    )
    assert len(slots) == 4, "an unsupported size should fall back, not break"


def test_a_generated_tab_stores_its_dimensions_not_its_slots(assets):
    """Storing slots lets a saved tab drift from what the generator produces."""
    js = assets["js"]
    block = js[js.index('$("newtab-create")'):js.index("function deleteCustomTab")]
    assert "cols: customCols" in block and "rows: customRows" in block


def test_a_generated_tab_is_rebuilt_from_those_dimensions(assets):
    js = assets["js"]
    block = js[js.index("function customToWorkspace"):js.index("function refreshWorkspaces")]
    assert "buildCustomTemplate(tab.cols" in block


def test_changing_the_size_selects_the_custom_grid(assets):
    """Otherwise adjusting the dimensions while a preset is selected looks
    like nothing happened."""
    js = assets["js"]
    block = js[js.index('$("template-grid").addEventListener'):
               js.index('$("panel-picker").addEventListener')]
    assert "draftTemplate = buildCustomTemplate(customCols, customRows)" in block


# --- the dialog has to fit on screen -----------------------------------------

def test_the_dialog_body_scrolls(assets):
    """Twenty templates plus nine panels is taller than a laptop viewport, and
    the picker was cut off with no way to reach it."""
    css = assets["css"]
    block = re.search(r"\n\.modal-body \{([^}]*)\}", css).group(1)
    assert "overflow-y: auto" in block


def test_the_body_can_actually_shrink(assets):
    """A flex child defaults to min-height:auto, refuses to shrink below its
    content, and pushes the overflow outside the card instead of scrolling."""
    css = assets["css"]
    block = re.search(r"\n\.modal-body \{([^}]*)\}", css).group(1)
    assert "min-height: 0" in block
    assert "flex: 1" in block


def test_the_create_button_is_outside_the_scroll_area(assets):
    """A create button below the fold on a dialog that does not scroll is a
    dead end."""
    html = assets["html"]
    body_start = html.index('<div class="modal-body">')
    body_end = html.index('<div class="modal-foot-actions">')
    assert html.index('id="newtab-create"') > body_end > body_start


def test_the_card_is_bounded_by_the_viewport(assets):
    css = assets["css"]
    block = re.search(r"\n\.modal-card \{([^}]*)\}", css).group(1)
    assert re.search(r"max-height:\s*\d+vh", block)


def test_the_picker_does_not_nest_a_second_scroller(assets):
    """Nested scroll areas make it unclear which one the wheel is driving."""
    css = assets["css"]
    tail = css[css.index(".panel-picker { max-height: none"):]
    assert "overflow: visible" in tail[:120]
