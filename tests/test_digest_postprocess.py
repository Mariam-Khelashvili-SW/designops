"""Daily Pulse post-processing (C1–C10)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from designops.pipelines.digest_postprocess import (
    _dedupe_escalations_by_issue,
    _format_field_lines,
    _group_no_report_rows,
    postprocess_digest,
)
from designops.pipelines.render import render_digest


def test_dedupe_leave_escalations_by_date():
    digest = {
        "escalations": [
            {
                "text": "Vlad out 14 Aug on Northerner — no cover",
                "who": "Vlad Shemetovets",
                "project": "Northerner",
                "agent_note": "no coverage arrangement appears in the dailies",
                "evidence": "— leave calendar",
            },
            {
                "text": "Predrag out 14 Aug on Northerner — no cover",
                "who": "Predrag Gavrilovikj",
                "project": "Northerner",
                "agent_note": "no coverage arrangement appears in the dailies",
                "evidence": "— daily report, 11 Aug",
            },
            {
                "text": "Arturs out 14 Aug on Acer — sole designer",
                "who": "Arturs Boroviks",
                "project": "Acer",
                "agent_note": "no coverage arrangement appears in the dailies",
                "evidence": "— leave calendar",
            },
        ],
        "status": [],
        "heads_ups": [],
        "no_report": [],
        "at_a_glance": {"active": 5},
    }
    _dedupe_escalations_by_issue(digest)
    assert len(digest["escalations"]) == 1
    assert "14 aug" in digest["escalations"][0]["text"].lower()
    assert len(digest["escalations"][0].get("affected") or []) == 3


def test_format_done_next_max_three_lines():
    text = (
        "Item one; Item two; Item three; Item four; "
        "call with Vlad; meeting with Joe"
    )
    lines = _format_field_lines(text, max_lines=3)
    assert len(lines) <= 3
    assert "call with Vlad" in lines[-1]


def test_group_no_report_rows():
    digest = {
        "no_report": [
            {"name": "Elene Chekurishvili", "status": "no_report", "context": None},
            {"name": "Kirill Rogovets", "status": "no_report", "context": None},
            {"name": "Tamari Giunashvili", "status": "no_report", "context": None},
            {"name": "Dorota Umiastowska", "status": "on_leave", "context": "On leave"},
        ]
    }
    _group_no_report_rows(digest)
    assert digest["no_report_grouped"]["label"].count("·") == 2
    assert len(digest["no_report"]) == 1
    assert digest["no_report"][0]["status"] == "on_leave"


def test_render_unified_notes_no_beyond_dailies():
    digest = {
        "at_a_glance": {
            "active": 2,
            "need_you": 0,
            "waiting_on_input": 1,
            "no_report": 0,
        },
        "escalations": [],
        "status": [
            {
                "person": "Vlad Shemetovets",
                "project": "Northerner",
                "done_lines": ["Adjusted cart.", "Updated PDP."],
                "next_lines": ["Minicart adjustments."],
            },
        ],
        "project_statuses": {"northerner": "on track"},
        "project_notes": {
            "northerner": [
                {
                    "type": "stalled",
                    "text": "'Minicart' has been Next for 3 runs.",
                    "sources": ["run-log 7–11 Aug"],
                },
                {
                    "type": "context",
                    "text": "Phase 2 prep is in motion.",
                    "sources": ["daily report, 10 Aug"],
                },
            ]
        },
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(digest, date(2026, 8, 11), sample=True)
    assert "Beyond the dailies" not in html
    assert "Agent note" not in html
    assert "Stalled" in html
    assert "Context" in html
    assert "waiting on input" in html


def test_out_and_quiet_matches_v3_layout():
    """11 Aug reference: on-leave row, grouped upcoming leave, grouped silent."""
    from designops.core.enums import PersonStatus
    from designops.pipelines.digest_postprocess import build_out_and_quiet, postprocess_digest

    report_date = date(2026, 8, 11)
    roster = [
        SimpleNamespace(
            full_name="Dorota Umiastowska",
            status=PersonStatus.ON_LEAVE,
            leave_from=date(2026, 8, 10),
            leave_until=date(2026, 8, 19),
        ),
        SimpleNamespace(
            full_name="Vlad Shemetovets",
            status=PersonStatus.ON_LEAVE,
            leave_from=date(2026, 8, 14),
            leave_until=date(2026, 9, 2),
        ),
        SimpleNamespace(
            full_name="Predrag Gavrilovikj",
            status=PersonStatus.ON_LEAVE,
            leave_from=date(2026, 8, 14),
            leave_until=date(2026, 8, 14),
        ),
        SimpleNamespace(
            full_name="Arturs Boroviks",
            status=PersonStatus.ON_LEAVE,
            leave_from=date(2026, 8, 14),
            leave_until=date(2026, 8, 14),
        ),
    ]
    digest = {
        "escalations": [
            {
                "text": "Coverage gap on 14 aug — no cover named",
                "affected": [
                    {"project": "Northerner", "who": "Vlad Shemetovets"},
                    {"project": "Acer", "who": "Arturs Boroviks"},
                ],
                "evidence": "leave calendar",
            }
        ],
        "no_report": [
            {"name": "Elene Chekurishvili", "status": "no_report", "context": None},
            {"name": "Kirill Rogovets", "status": "no_report", "context": None},
            {"name": "Tamari Giunashvili", "status": "no_report", "context": None},
        ],
        "status": [],
        "at_a_glance": {"active": 5},
    }
    postprocess_digest(digest, report_date=report_date, roster_rows=roster)
    rows = digest["out_and_quiet"]
    assert len(rows) == 3
    assert rows[0]["kind"] == "on_leave"
    assert rows[0]["label"] == "Dorota Umiastowska"
    assert rows[0]["detail"] == "On leave 10 → 19 Aug"
    assert rows[1]["kind"] == "upcoming"
    assert "From Fri 14 Aug" in rows[1]["label"]
    assert "Vlad (→ 2 Sep)" in rows[1]["label"]
    assert "Predrag (1 d)" in rows[1]["label"]
    assert "Arturs (1 d)" in rows[1]["label"]
    assert rows[1]["detail"] == "see item 1"
    assert rows[2]["kind"] == "silent"
    assert "Elene Chekurishvili" in rows[2]["label"]

    html = render_digest(digest, report_date, sample=True)
    assert "On leave 10 → 19 Aug" in html
    assert "From Fri 14 Aug" in html
    assert "see item 1" in html


def test_olga_review_detection_and_status():
    roster = [
        SimpleNamespace(
            full_name="Arturs Boroviks",
            leave_from=date(2026, 8, 14),
            leave_until=date(2026, 8, 14),
            status="active",
            leave_from_=None,
        )
    ]
    digest = {
        "escalations": [],
        "heads_ups": [],
        "status": [
            {
                "person": "Arturs Boroviks",
                "project": "Acer",
                "done": "Shared updates with Olga for internal review.",
                "next": "Catch-up with Olga for further steps.",
            }
        ],
        "no_report": [],
        "at_a_glance": {"active": 1},
    }
    postprocess_digest(
        digest,
        report_date=date(2026, 8, 11),
        roster_rows=roster,
    )
    assert any("review" in (e.get("text") or "").lower() for e in digest["escalations"])
    assert digest["project_statuses"].get("acer") == "waiting on you"
