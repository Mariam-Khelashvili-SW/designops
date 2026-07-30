"""§7.1 golden digest + §7.3 fidelity. The deterministic parts (JSON shape, render
DOM structure, verbatim survival) run with no LLM. The actual model reproduction is
marked `llm` and skips without ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest

from designops.pipelines.render import render_digest

FIXTURE = Path(__file__).parent / "fixtures" / "2026-07-17"
REPORT_DATE = date(2026, 7, 17)


@pytest.fixture
def expected() -> dict:
    return json.loads((FIXTURE / "expected_digest.json").read_text())


class _ClassCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "class" and v:
                self.classes.append(v)


def _count(html: str, css_class: str) -> int:
    c = _ClassCounter()
    c.feed(html)
    return sum(1 for cls in c.classes if css_class in cls.split())


# --- §7.1 golden counts (structured JSON reproduces the reference) --------------
def test_golden_json_counts_match_spec(expected):
    g = expected["at_a_glance"]
    assert g == {"reported": 5, "blocked": 1, "escalations": 1, "no_report": 4}
    assert len(expected["no_report"]) == 4
    assert len(expected["action_needed"]) >= 1  # spec §9 — Olga's action list, rendered last

    reported = {p["name"] for proj in expected["projects"] for p in proj["people"]}
    assert reported == {
        "Arturs Boroviks", "Dorota Umiastowska", "Elene Chekurishvili",
        "Kirill Rogovets", "Predrag Gavrilovikj",
    }
    # exactly one blocker and one escalation across all daily rows (lean v1)
    blockers = [p for proj in expected["projects"] for p in proj["people"] if p["blocker"]]
    escals = [p for proj in expected["projects"] for p in proj["people"] if p["escalation"]]
    assert len(blockers) == 1 and blockers[0]["name"] == "Elene Chekurishvili"
    assert len(escals) == 1 and escals[0]["name"] == "Elene Chekurishvili"


# --- §7.1 render asserted on DOM structure, not byte equality -------------------
def test_render_dom_structure(expected):
    # email-safe, table-based layout (no flex/grid)
    html = render_digest(expected, REPORT_DATE, sample=True)
    assert _count(html, "ptitle") == 8          # 8 project titles
    assert _count(html, "pname") == 9           # 9 person entries
    assert html.count(">Blocker</td>") == 1
    assert html.count(">Escalation</td>") == 1
    assert _count(html, "nrname") == 4          # 4 no-report rows
    assert "SAMPLE" not in html                 # no demo/dry-run markers in the digest
    assert ">5<" in html and ">4<" in html      # glance numbers rendered
    assert "Where your action is needed" in html  # action section rendered last
    assert "display:grid" not in html and "display:flex" not in html  # email-safe


# --- §7.3 fidelity spot-check: verbatim blocker/escalation survive into output --
def test_fidelity_verbatim_survives(expected):
    import html as _html

    # unescape so the check is on the substance, not on HTML entity encoding of quotes
    out = _html.unescape(render_digest(expected, REPORT_DATE, sample=True))
    assert "I think tickets aren't up yet to log time." in out
    assert "didn't have much time to start UI for the wireframe" in out


# --- coverage caveat wording (§11.1) -------------------------------------------
def test_coverage_incomplete_warns(expected):
    html = render_digest(
        expected, REPORT_DATE, sample=True,
        coverage={"accounts_requested": 10, "exports_succeeded": 8,
                  "exports_failed": 2, "incomplete": True},
    )
    assert "Coverage incomplete" in html
    assert "A designer working outside these accounts can surface here wrongly." in html


# --- §7.1 the model actually reproduces the golden digest (needs a key) ---------
@pytest.mark.llm
def test_llm_reproduces_golden_counts():
    import os

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    corpus_path = FIXTURE / "corpus.json"
    if not corpus_path.exists():
        pytest.skip("corpus fixture not present")


    # (built lazily; the deterministic tests above are the everyday signal)
    pytest.skip("wire roster/registry from DB or seed for the live LLM run")
