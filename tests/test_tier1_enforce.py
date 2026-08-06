"""Tier-1 Growth-Pulse enforcement: review requires link; no invented progress."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from designops.pipelines.daily_digest import _enforce_structure, _working_review_link


def test_working_review_link_expands_issue_key(monkeypatch):
    from designops.pipelines import daily_digest as dd

    monkeypatch.setattr(
        dd,
        "get_settings",
        lambda: SimpleNamespace(jira_base_url="https://scandiflow.atlassian.net"),
    )
    assert (
        _working_review_link("uom-482")
        == "https://scandiflow.atlassian.net/browse/UOM-482"
    )
    assert _working_review_link("https://example.com/x") == "https://example.com/x"
    assert _working_review_link(None) is None
    assert _working_review_link("") is None
    assert _working_review_link("DCP1") is None  # bare project key — not enough


def test_enforce_drops_linkless_review_and_unowned_questions():
    reported_id = uuid4()
    silent_id = uuid4()
    roster = [
        SimpleNamespace(id=reported_id, full_name="Dorota Umiastowska"),
        SimpleNamespace(id=silent_id, full_name="Elene Chekurishvili"),
    ]
    filtered = SimpleNamespace(reported_person_ids={reported_id})
    digest = {
        "status": [
            {"person": "Dorota Umiastowska", "project": "Acer", "done": "Adjusted CMS.", "next": "My Account."},
            {"person": "Elene Chekurishvili", "project": "FT", "done": "Invented.", "next": ""},
        ],
        "todays_plans": [
            {"person": "Dorota Umiastowska", "plan": "Club Portal sync."},
            {"person": "Elene Chekurishvili", "plan": "Should not keep."},
        ],
        "needs_review": [
            {
                "item": "No ticket link — drop me",
                "link": None,
                "who": "Elene Chekurishvili",
                "blocked": True,
            },
            {
                "item": "Sign-off needed",
                "link": "UOM-482",
                "who": "Kirill Rogovets",
                "blocked": False,
            },
        ],
        "open_questions": [
            {"question": "Can we ship?", "who": "Dorota Umiastowska"},
            {
                "question": "Which pages go live?",
                "who": "Mariam Makharadze",
                "project": "Enmedify",
                "evidence": 'email "Preparing for Phase 2 go live", 5 Aug',
                "link": "MEDPPC-12",
            },
            {"question": "Orphan ask", "who": ""},
        ],
    }
    _enforce_structure(digest, filtered, roster)
    assert [s["person"] for s in digest["status"]] == ["Dorota Umiastowska"]
    assert digest["status"][0]["done"] == "Adjusted CMS."
    assert digest["status"][0]["next"] == "My Account."
    assert [p["person"] for p in digest["todays_plans"]] == ["Dorota Umiastowska"]
    assert len(digest["needs_review"]) == 1
    assert digest["needs_review"][0]["link"].endswith("UOM-482") or digest["needs_review"][0][
        "link"
    ] == "UOM-482"
    assert len(digest["open_questions"]) == 2
    assert digest["open_questions"][0]["who"] == "Dorota Umiastowska"
    q2 = digest["open_questions"][1]
    assert q2["who"] == "Mariam Makharadze"
    assert q2["evidence"].startswith("email")
    assert q2["link"].endswith("MEDPPC-12") or q2["link"] == "MEDPPC-12"
    assert digest.get("escalations") == []
    assert digest.get("heads_ups") == []
    assert "signals" in digest
