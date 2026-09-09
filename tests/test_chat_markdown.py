"""The analyst's replies are Markdown, and Markdown is model output.

Rendered as plain text it was a wall of ### and **. Rendered carelessly it is
an injection vector. The renderer escapes the whole reply FIRST and then
formats the escaped text, so every tag in the output is one it wrote itself.
These tests pin both halves: that it formats, and that nothing survives escaping.
"""

import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.main import create_app


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    """Run the real renderer under node, so this tests the shipped code."""
    app = create_app(cfg=Config(db_path=tmp_path_factory.mktemp("db") / "t.db"),
                     start_poller=False)
    with TestClient(app) as c:
        js = c.get("/static/app.js").text

    # Lift only the pieces the renderer needs; the rest of app.js touches the DOM.
    wanted = []
    for name in ("const ESCAPE_MAP", "function escapeHtml",
                 "function inlineMarkdown", "function renderMarkdown"):
        start = js.index(name)
        end = js.index("\n}\n", start) + 3 if name.startswith("function") \
            else js.index("\n", start) + 1
        wanted.append(js[start:end])
    harness = "\n".join(wanted) + """
const input = require("fs").readFileSync(0, "utf8");
process.stdout.write(renderMarkdown(input));
"""
    script = tmp_path_factory.mktemp("js") / "render.js"
    script.write_text(harness, encoding="utf-8")

    def run(markdown):
        return subprocess.run(
            ["node", str(script)], input=markdown, capture_output=True, text=True, encoding="utf-8",
            check=True,
        ).stdout

    return run


# --- it formats --------------------------------------------------------------

def test_headings_become_headings(render):
    assert "<h3>Valuation</h3>" in render("### Valuation")


def test_bold_becomes_strong(render):
    assert "<strong>no</strong>" in render("It is **no**.")


def test_bullets_become_a_list(render):
    out = render("- one\n- two")
    assert out.count("<li>") == 2
    assert "<ul>" in out and "</ul>" in out


def test_numbered_items_become_an_ordered_list(render):
    out = render("1. first\n2. second")
    assert "<ol>" in out and out.count("<li>") == 2


def test_a_list_closes_before_a_paragraph(render):
    out = render("- one\n\nAfter.")
    assert out.index("</ul>") < out.index("<p>After.</p>")


def test_switching_list_type_closes_the_first(render):
    out = render("- bullet\n1. number")
    assert "</ul>" in out and "<ol>" in out
    assert out.index("</ul>") < out.index("<ol>")


def test_tables_become_tables(render):
    out = render("| Grade | Win |\n|---|---|\n| A+ | 42% |")
    assert "<table" in out
    assert "<th>Grade</th>" in out
    assert "<td>42%</td>" in out
    assert "---" not in out, "the separator row leaked into the body"


def test_a_table_scrolls_rather_than_bursting_the_panel(render):
    assert "md-table-wrap" in render("| a | b |\n|---|---|\n| 1 | 2 |")


def test_inline_code_survives_intact(render):
    assert "<code>ATR</code>" in render("uses `ATR` for the stop")


def test_code_contents_are_not_re_parsed(render):
    """Otherwise `**x**` inside code renders as bold instead of literal."""
    out = render("literal `**not bold**` here")
    assert "<code>**not bold**</code>" in out


def test_paragraphs_are_separated(render):
    out = render("First para.\n\nSecond para.")
    assert out.count("<p>") == 2


def test_a_wrapped_paragraph_joins_into_one(render):
    out = render("A sentence that\ncontinues on the next line.")
    assert out.count("<p>") == 1
    assert "that continues" in out


def test_a_rule_becomes_a_rule(render):
    assert "<hr>" in render("above\n\n---\n\nbelow")


def test_http_links_are_rendered(render):
    out = render("see [the filing](https://example.com/a)")
    assert '<a href="https://example.com/a"' in out
    assert ">the filing</a>" in out


# --- it cannot inject --------------------------------------------------------

def test_raw_html_is_neutralised(render):
    out = render("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_an_img_onerror_payload_is_neutralised(render):
    out = render('<img src=x onerror="alert(1)">')
    assert "<img" not in out
    assert "onerror" not in out or "&lt;img" in out


def test_a_javascript_url_is_not_linked(render):
    """The scheme check is the only thing between a rendered link and an
    executable one."""
    out = render("[click](javascript:alert(1))")
    assert "<a href" not in out
    assert "javascript:alert" in out  # shown as text, harmless


def test_a_data_url_is_not_linked(render):
    out = render("[x](data:text/html;base64,PHNjcmlwdD4=)")
    assert "<a href" not in out


def test_a_quote_cannot_break_out_of_an_href(render):
    out = render('[x](https://a.test/")  onmouseover="alert(1))')
    assert "onmouseover=" not in out.split("</a>")[0] if "</a>" in out else True
    assert "&quot;" in out or "<a href" not in out


def test_html_inside_a_heading_is_escaped(render):
    out = render("### <b>bold</b>")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_html_inside_a_table_cell_is_escaped(render):
    out = render("| a |\n|---|\n| <script>x</script> |")
    assert "<script>" not in out


def test_html_inside_a_list_item_is_escaped(render):
    assert "<script>" not in render("- <script>x</script>")


def test_the_renderer_escapes_before_it_formats(render):
    """The whole safety argument in one line: the model's angle brackets stop
    being angle brackets before any pattern runs."""
    js_src = subprocess.run(
        ["grep", "-n", "escapeHtml(String(raw", "frontend/app.js"],
        capture_output=True, text=True, encoding="utf-8").stdout
    assert js_src, "renderMarkdown must escape its input first"


# --- it does not mangle ordinary text ----------------------------------------

def test_an_ampersand_renders_once(render):
    out = render("Profit & loss")
    assert "&amp;" in out
    assert "&amp;amp;" not in out, "double-escaped"


def test_a_lone_asterisk_is_not_italic(render):
    out = render("2 * 3 = 6")
    assert "<em>" not in out


def test_empty_input_is_empty_output(render):
    assert render("").strip() == ""


def test_plain_prose_still_renders(render):
    assert "<p>" in render("Just an ordinary sentence.")
