"""§7.1 golden Growth-Pulse digest + §7.3 fidelity. Deterministic parts (JSON shape,
render DOM, verbatim survival) run with no LLM.
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


def test_golden_json_counts_match_spec(expected):
    g = expected["at_a_glance"]
    assert g == {"active": 5, "need_review": 2, "blocked": 0, "no_report": 4}
    assert len(expected["no_report"]) == 4
    assert len(expected["needs_review"]) == 2
    assert expected["open_questions"] == []
    assert expected.get("escalations") == []
    assert expected.get("heads_ups") == []
    # Tier-1: every review item has a working link
    assert all(r.get("link") for r in expected["needs_review"])

    people = {s["person"] for s in expected["status"]}
    assert people == {
        "Arturs Boroviks",
        "Dorota Umiastowska",
        "Elene Chekurishvili",
        "Kirill Rogovets",
        "Predrag Gavrilovikj",
    }


def test_render_dom_structure(expected):
    html = render_digest(expected, REPORT_DATE, sample=True)
    assert "Daily Pulse" in html
    assert "needs you" in html
    assert "By project" in html
    assert "Needs attention" in html
    assert "Other plans" in html  # cross-project Club Portal line
    assert "Out &amp; quiet" in html
    # Quiet-day rule: empty open_questions → no standalone questions section
    assert "Open questions" not in html
    for nr in expected["no_report"]:
        assert nr["name"] in html
    assert "SAMPLE" not in html
    assert "V2 PREVIEW" not in html
    assert "not sent • UX/UI" not in html
    assert ">5<" in html
    assert "display:grid" not in html and "display:flex" not in html


def test_fidelity_verbatim_survives(expected):
    import html as _html

    out = _html.unescape(render_digest(expected, REPORT_DATE, sample=True))
    assert "further than" in out
    assert "Base Template needs your sign-off" in out


def test_quiet_day_omits_empty_sections():
    quiet = {
        "at_a_glance": {"active": 2, "need_review": 0, "blocked": 0, "no_report": 0},
        "status": [
            {"person": "A", "project": "X", "done": "Shipped a small fix.", "next": "Continue X."},
            {"person": "B", "project": "Y", "done": "Sync + polish.", "next": ""},
        ],
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(quiet, REPORT_DATE, sample=True)
    assert "By project" in html
    assert "Done" in html
    assert "Next" in html
    assert "Needs attention" not in html
    assert "Out &amp; quiet" not in html
    # KPI still shows the no-report count label
    assert html.count("no report") == 1


def test_render_groups_by_project_with_done_next():
    digest = {
        "at_a_glance": {"active": 2, "need_review": 0, "blocked": 0, "no_report": 0},
        "status": [
            {"person": "Arturs Boroviks", "project": "Acer", "done": "PLP work.", "next": "Kick-off."},
            {"person": "Dorota Umiastowska", "project": "Acer", "done": "CMS tweaks.", "next": ""},
            {"person": "Dorota Umiastowska", "project": "SGD", "done": "Finished PLP.", "next": "PDP."},
        ],
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(digest, REPORT_DATE, sample=True)
    # Project headers before people
    acer_i = html.index("Acer")
    sgd_i = html.index("SGD")
    assert acer_i < sgd_i
    assert html.index("Arturs Boroviks") > acer_i
    assert "PLP work." in html
    assert "Kick-off." in html
    assert "Finished PLP." in html


def test_coverage_incomplete_warns(expected):
    html = render_digest(
        expected,
        REPORT_DATE,
        sample=True,
        coverage={
            "accounts_requested": 10,
            "exports_succeeded": 8,
            "exports_failed": 2,
            "incomplete": True,
        },
    )
    # Coverage is audit-only (run page / flags) — not shown in the email body.
    assert "Fairwind:" not in html
    assert "export(s) failed" not in html


@pytest.mark.llm
def test_llm_reproduces_golden_counts():
    import os

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    corpus_path = FIXTURE / "corpus.json"
    if not corpus_path.exists():
        pytest.skip("corpus fixture not present")
    pytest.skip("wire roster/registry from DB or seed for the live LLM run")
