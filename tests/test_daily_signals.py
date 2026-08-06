"""Intelligence-layer signal validation (R1–R6) and digest render."""

from __future__ import annotations

from datetime import date

import pytest

from designops.pipelines.daily_signals import (
    SIGNAL_KEYS,
    attach_agent_notes,
    empty_signals,
    enforce_intelligence_artifacts,
    materialize_intelligence,
    validate_signals,
)
from designops.pipelines.render import render_digest
from designops.pipelines.synthesis import build_system_prompt, build_user_content


def test_prune_keeps_compound_intelligence_project_names():
    """LLM sometimes emits 'Acer / Club Portal' — must not drop the leave escalation."""
    from designops.pipelines.synthesis import _prune_ungrounded

    digest = {
        "status": [
            {"person": "Dorota Umiastowska", "project": "Acer", "done": "SRP", "next": ""}
        ],
        "needs_review": [],
        "open_questions": [],
        "escalations": [
            {
                "text": "Dorota leave while mid-flight on Acer and Club Portal.",
                "evidence": "— leave calendar × dailies",
                "why_ranked_here": "starts tomorrow",
                "project": "Acer / Club Portal (SGDCP)",
                "who": "Dorota Umiastowska",
            },
            {
                "text": "Fake project never mentioned.",
                "evidence": "— invent",
                "why_ranked_here": "x",
                "project": "Totally Invented Client XYZ",
            },
        ],
        "heads_ups": [
            {
                "text": "Go-live prep",
                "evidence": "— daily",
                "project": "Enmedify",
            }
        ],
        "at_a_glance": {},
    }
    corpus = (
        "## Daily reports\n### Dorota Umiastowska\n"
        "- [fairwind · project: Acer] daily\n  search UI\n"
        "- [fairwind · project: Club Portal] daily\n  wireframes\n"
        "- [fairwind · project: Enmedify] daily\n  go live\n"
    )
    projects = [
        {"canonical_name": "Acer", "aliases": []},
        {"canonical_name": "Sports Group Denmark", "aliases": ["Club Portal", "SGDCP"]},
        {"canonical_name": "Enmedify", "aliases": []},
    ]
    _prune_ungrounded(digest, corpus, projects)
    assert len(digest["escalations"]) == 1
    assert "Acer / Club Portal" in digest["escalations"][0]["project"]
    assert len(digest["heads_ups"]) == 1


def test_empty_signals_has_all_keys_with_checked():
    s = empty_signals()
    assert set(s) == set(SIGNAL_KEYS)
    for k in SIGNAL_KEYS:
        assert s[k]["findings"] == []
        assert s[k]["checked"]


def test_validate_signals_requires_all_keys():
    with pytest.raises(ValueError, match="missing required key"):
        validate_signals({"R1_leave_x_assignment": {"findings": [], "checked": "x"}})


def test_validate_signals_empty_needs_checked():
    raw = empty_signals()
    raw["R1_leave_x_assignment"] = {"findings": []}
    with pytest.raises(ValueError, match="checked"):
        validate_signals(raw)


def test_validate_signals_drops_findings_without_evidence():
    raw = empty_signals()
    raw["R1_leave_x_assignment"] = {
        "findings": [
            {
                "kind": "escalation",
                "text": "Coverage gap on Acer",
                "why_ranked_here": "leave tomorrow",
                "evidence": [],
            }
        ],
        "checked": "should not keep empty-evidence finding",
    }
    out = validate_signals(raw)
    assert out["R1_leave_x_assignment"]["findings"] == []
    assert out["R1_leave_x_assignment"]["checked"]


def test_validate_signals_blocklist_outside_quotes():
    raw = empty_signals()
    raw["R4_repeated_next"] = {
        "findings": [
            {
                "kind": "agent_note",
                "text": "Work is stalled on the homepage",
                "who": "Kirill Rogovets",
                "evidence": [{"quote": "Finish HP", "source": "run-log"}],
            }
        ]
    }
    with pytest.raises(ValueError, match="blocklisted"):
        validate_signals(raw, roster_names={"Kirill Rogovets"})


def test_validate_signals_allows_blocklist_inside_quotes():
    raw = empty_signals()
    raw["R5_report_language"] = {
        "findings": [
            {
                "kind": "escalation",
                "text": 'Person wrote "blocked on access"',
                "who": "Kirill Rogovets",
                "why_ranked_here": "stop language",
                "evidence": [{"quote": "blocked on access", "source": "daily report"}],
            }
        ]
    }
    out = validate_signals(raw, roster_names={"Kirill Rogovets"})
    assert len(out["R5_report_language"]["findings"]) == 1


def test_materialize_caps_escalations():
    findings = []
    for i in range(7):
        findings.append(
            {
                "kind": "escalation",
                "text": f"Escalation {i}",
                "why_ranked_here": f"rank {i}",
                "evidence": [{"quote": f"q{i}", "source": "daily"}],
            }
        )
    signals = empty_signals()
    signals["R1_leave_x_assignment"] = {"findings": findings}
    esc, hu, notes = materialize_intelligence(signals)
    assert len(esc) == 6  # 5 + hold line
    assert esc[-1]["text"].startswith("+2 lower-priority")
    assert hu == []
    assert notes == []


def test_enforce_intelligence_and_attach_notes():
    digest = {
        "escalations": [
            {
                "text": "Dorota leave mid-flight on Acer",
                "evidence": "leave calendar × dailies",
                "why_ranked_here": "starts tomorrow",
                "who": "Dorota Umiastowska",
                "project": "Acer",
                "agent_note": "no coverage arrangement appears in the dailies",
            },
            {"text": "", "evidence": "x"},
            {"text": "No evidence row", "evidence": ""},
        ],
        "heads_ups": [
            {
                "text": "Enmedify go-live phase 2",
                "evidence": "— daily report, 4 Aug",
                "project": "Enmedify",
            }
        ],
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Base template",
                "next": "Finish HP",
            }
        ],
    }
    enforce_intelligence_artifacts(digest)
    assert len(digest["escalations"]) == 1
    assert digest["escalations"][0]["evidence"].startswith("—")
    assert len(digest["heads_ups"]) == 1
    attach_agent_notes(
        digest["status"],
        [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "text": "'Finish HP' has been Next since 14 Jul — 4th run.",
                "evidence": "— run-log",
            }
        ],
    )
    assert "4th run" in digest["status"][0]["agent_note"]


def test_build_user_content_includes_leave_and_prior_next():
    from types import SimpleNamespace

    filtered = SimpleNamespace(included=[], beyond_daily=[])
    text = build_user_content(
        filtered,
        leave_calendar=[
            {
                "full_name": "Dorota Umiastowska",
                "leave_from": "2026-08-05",
                "leave_until": "2026-08-07",
                "status": "active",
            }
        ],
        prior_next=[
            {
                "report_date": "2026-08-03",
                "next_items": [
                    {
                        "person": "Kirill Rogovets",
                        "project": "UoM",
                        "next": "Finish HP",
                    }
                ],
            }
        ],
    )
    assert "Leave calendar" in text
    assert "Dorota Umiastowska" in text
    assert "2026-08-05" in text
    assert "Prior Next run-log" in text
    assert "Finish HP" in text


def test_system_prompt_pass_a_vs_b():
    roster = [{"full_name": "A", "status": "active"}]
    projects = [{"canonical_name": "Acer", "aliases": []}]
    a = build_system_prompt(date(2026, 8, 4), roster, projects, pass_label="A")
    b = build_system_prompt(date(2026, 8, 4), roster, projects, pass_label="B")
    assert "Pass A only" in a
    assert "Pass B only" in b
    assert "Leave × active assignment" in a or "R1" in a


def test_dedupe_heads_up_against_escalation():
    from designops.pipelines.daily_signals import empty_signals, materialize_intelligence

    signals = empty_signals()
    signals["R1_leave_x_assignment"] = {
        "findings": [
            {
                "kind": "escalation",
                "text": "Dorota on leave from tomorrow while mid-flight on Acer and SGD wireframes.",
                "who": "Dorota Umiastowska",
                "project": "Acer",
                "why_ranked_here": "leave tomorrow",
                "evidence": [{"quote": "leave", "source": "leave calendar"}],
            }
        ]
    }
    signals["R2_client_wait_x_leave"] = {
        "findings": [
            {
                "kind": "heads_up",
                # Overlaps heavily with the escalation — should be dropped
                "text": "Dorota on leave from tomorrow while mid-flight on Acer and SGD wireframes awaiting feedback.",
                "who": "Dorota Umiastowska",
                "project": "Acer",
                "evidence": [{"quote": "sent for review", "source": "daily"}],
            },
            {
                "kind": "heads_up",
                "text": "SGD wireframes sent for client review; feedback will queue until she returns.",
                "who": "Dorota Umiastowska",
                "project": "Sports Group Denmark",
                "evidence": [{"quote": "sent for review", "source": "daily"}],
            },
        ]
    }
    esc, hu, _ = materialize_intelligence(signals)
    assert len(esc) == 1
    assert len(hu) == 1
    assert "SGD wireframes sent" in hu[0]["text"]


def test_render_intelligence_no_v2_preview_banner():
    digest = {
        "at_a_glance": {"active": 1, "need_review": 1, "blocked": 0, "no_report": 0},
        "escalations": [
            {
                "text": "Dorota on leave from tomorrow while mid-flight on Acer.",
                "evidence": "— leave calendar × dailies",
                "why_ranked_here": "leave starts tomorrow",
                "who": "Dorota Umiastowska",
                "project": "Acer",
                "agent_note": "no coverage arrangement appears in the dailies",
            }
        ],
        "heads_ups": [
            {
                "text": "Enmedify go-live phase 2 preparation.",
                "evidence": "— daily report, 4 Aug",
                "project": "Enmedify",
            }
        ],
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Continued Base Template.",
                "next": "Finish HP",
                "agent_note": "'Finish HP' has been Next since 14 Jul — 4th run.",
            },
            {
                "person": "Mariam Makharadze",
                "project": "Enmedify",
                "done": "Prep for go-live.",
                "next": "Phase 2 checklist.",
            },
        ],
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(digest, date(2026, 8, 4), sample=True)
    assert "Needs Olga" in html
    assert "Dorota on leave" in html
    assert "Enmedify go-live" in html
    # Heads-up lives under the project in Beyond the dailies, not a top-level section
    assert html.index("By project") < html.index("Enmedify go-live")
    assert html.index("Enmedify") < html.index("Enmedify go-live")
    assert "Beyond the dailies" in html
    # Prototype: heads-up sits below that project's Done/Next, not above
    assert html.index("Phase 2 checklist") < html.index("Enmedify go-live")
    # No standalone top Heads-up section heading before By project
    top = html.split("By project")[0]
    assert "<h2>Heads-up</h2>" not in top
    assert "⚙" in html
    assert "4th run" in html
    assert "V2 PREVIEW" not in html
    assert "not sent • UX/UI" not in html
    assert "SAMPLE / DRY-RUN" not in html
    assert "SAMPLE" not in html
