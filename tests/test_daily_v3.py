"""Daily Pulse v3 — person split, repeat detection, Jira worklog rows."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from designops.adapters.documents import Document
from designops.pipelines.daily_repeats import (
    apply_repeat_kpis,
    detect_repeats,
    intents_match,
    normalize_intent,
)
from designops.pipelines.daily_worklogs import (
    TicketRow,
    WorklogBundle,
    _rows_from_tempo,
    attach_worklogs,
    is_design_owned_ticket,
    zero_logged_range_label,
)
from designops.pipelines.render import render_digest


def test_normalize_intent_strips_filler():
    a = normalize_intent("try to wrap up Shopping cart & Mini cart")
    b = normalize_intent("wrap up Shopping cart & Mini cart")
    assert a == b
    assert intents_match(a, b)
    c = normalize_intent("continue work on global elements")
    d = normalize_intent("work on global elements")
    assert intents_match(c, d)


def test_repeat_flags_at_three_days_not_two():
    report = date(2026, 8, 12)
    history = [
        {
            "report_date": date(2026, 8, 11),
            "person": "Kirill Rogovets",
            "project": "University of Michigan",
            "next": "try to finalise Minicart & Cart page",
            "done": "Worked on minicart.",
        },
        {
            "report_date": date(2026, 8, 10),
            "person": "Kirill Rogovets",
            "project": "University of Michigan",
            "next": "Finalise Minicart & Cart page",
            "done": "Started minicart.",
        },
    ]
    digest = {
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Worked on Minicart.",
                "next": "Finalise Minicart & Cart page; finalise checkout.",
                "ticket_hours": 4.0,
                "tickets": [
                    {
                        "key": "UOM-512",
                        "status": "In Progress",
                        "spent_hours": 11.5,
                        "original_hours": 6,
                    }
                ],
            }
        ],
        "escalations": [],
        "at_a_glance": {},
    }
    detect_repeats(digest, report_date=report, history=history)
    apply_repeat_kpis(digest)
    assert digest["repeat_check"] == "ok"
    assert digest["status"][0]["repeat"]["streak"] == 3
    assert "Kirill Rogovets" in digest["repeating_people"]
    assert digest["at_a_glance"]["repeating"] == 1
    assert "UOM-512" in digest["status"][0]["repeat"]["tail"]

    # Two days only — no flag unless waiting on Olga.
    digest2 = {
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Worked on Minicart.",
                "next": "Finalise Minicart & Cart page",
            }
        ],
        "escalations": [],
    }
    detect_repeats(
        digest2,
        report_date=report,
        history=[history[0]],
    )
    assert "repeat" not in digest2["status"][0]


def test_repeat_two_days_behind_olga_is_watch():
    report = date(2026, 8, 12)
    digest = {
        "status": [
            {
                "person": "Maarja Truu",
                "project": "The Universal Group",
                "done": "No design progress reported today (planned for tomorrow).",
                "next": "Adjust designs based on Olga's feedback.",
                "_waiting_on_olga": True,
                "ticket_hours": 0,
            }
        ],
        "escalations": [],
    }
    history = [
        {
            "report_date": date(2026, 8, 11),
            "person": "Maarja Truu",
            "project": "The Universal Group",
            "next": "Adjust designs based on Olga's feedback.",
            "done": "No design progress reported today.",
        }
    ]
    detect_repeats(digest, report_date=report, history=history)
    assert digest["status"][0]["repeat"]["streak"] == 2
    assert digest["repeating_people"] == []  # KPI is 3+ people only


def test_repeat_skips_weekend_and_leave():
    report = date(2026, 8, 12)  # Wednesday
    # Mon 10, skip weekend 8–9, Fri 7 — three reporting days if Fri+Mon+Wed match
    # Actually streak: Wed 12, skip nothing to Tue 11 (no hist → break) unless
    # we have Thu? 12 Wed, 11 Tue, 10 Mon. Weekend before that is 8–9.
    person = SimpleNamespace(
        full_name="Vlad Shemetovets",
        status="active",
        leave_from=date(2026, 8, 11),
        leave_until=date(2026, 8, 11),
    )
    digest = {
        "status": [
            {
                "person": "Vlad Shemetovets",
                "project": "Northerner",
                "done": "Cart work.",
                "next": "wrap up Shopping cart & Mini cart",
            }
        ],
        "escalations": [],
    }
    history = [
        {
            "report_date": date(2026, 8, 10),
            "person": "Vlad Shemetovets",
            "project": "Northerner",
            "next": "try to wrap up Shopping cart & Mini cart",
            "done": "Started cart.",
        },
        {
            "report_date": date(2026, 8, 7),
            "person": "Vlad Shemetovets",
            "project": "Northerner",
            "next": "wrap up Shopping cart & Mini cart",
            "done": "Cart.",
        },
    ]
    detect_repeats(digest, report_date=report, history=history, roster_rows=[person])
    # 12 Wed + skip leave Tue 11 + Mon 10 + skip weekend + Fri 7 = 3 days
    assert digest["status"][0]["repeat"]["streak"] == 3


def test_repeat_unavailable_without_history():
    digest = {"status": [{"person": "A", "project": "X", "next": "Finish HP", "done": "x"}]}
    detect_repeats(digest, report_date=date(2026, 8, 12), history=[])
    assert digest["repeat_check"] == "unavailable"
    assert "repeat" not in digest["status"][0]
    detect_repeats(digest, report_date=date(2026, 8, 12), history=None)
    assert "no history" in digest["repeat_note"]


def test_attach_worklogs_and_no_ticket_found():
    report = date(2026, 8, 12)
    doc = Document(
        source="jira",
        external_id="UOM-512",
        event_date=report,
        author_identity="acc-1",
        title="UOM-512: Minicart",
        body="Status: In Progress",
        url="https://scandiweb.atlassian.net/browse/UOM-512",
        raw={
            "key": "UOM-512",
            "summary": "Minicart & Cart page — design",
            "status": "In Progress",
            "project_key": "UOM",
            "project_name": "University of Michigan",
            "original_hours": 6,
            "spent_hours": 11.5,
            "status_entries": [{"to": "In Progress", "at": "2026-08-12T09:00:00.000+0000"}],
        },
    )
    bundle = WorklogBundle(
        available=True,
        tickets=[
            (
                "Kirill Rogovets",
                TicketRow(
                    key="UOM-512",
                    summary="Minicart & Cart page — design",
                    status="In Progress",
                    hours=4.0,
                    url=doc.url,
                    stale_days=None,
                    project_key="UOM",
                    project_name="University of Michigan",
                    original_hours=6,
                    spent_hours=11.5,
                ),
            )
        ],
        hours_by_person={"Kirill Rogovets": 4.0},
    )
    digest = {
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Worked on minicart.",
                "next": "Finalise minicart.",
            },
            {
                "person": "Tamari Giunashvili",
                "project": "Tobis UAB",
                "done": "No progress reported today.",
                "next": "Follow up with the client.",
            },
        ]
    }
    attach_worklogs(digest, bundle, report_date=report)
    k = digest["status"][0]
    assert k["tickets"][0]["key"] == "UOM-512"
    assert k["tickets"][0]["hours"] == 4.0
    assert k["ticket_hours"] == 4.0
    t = digest["status"][1]
    assert t["no_ticket_found"] is True
    assert t["no_time_logged"]
    assert digest["hours_by_person"]["Kirill Rogovets"] == 4.0


def test_rows_from_tempo_resolves_issue_ids(monkeypatch):
    report = date(2026, 8, 13)
    doc = Document(
        source="jira",
        external_id="UOM-512",
        event_date=report,
        author_identity="acc-k",
        title="UOM-512: Minicart",
        body="Status: In Progress",
        url="https://scandiweb.atlassian.net/browse/UOM-512",
        raw={
            "id": "385969",
            "key": "UOM-512",
            "summary": "Minicart",
            "status": "In Progress",
            "project_key": "UOM",
            "status_entries": [],
        },
    )

    class FakeTempo:
        def list_worklogs(self, **kwargs):
            return [
                {
                    "account_id": "acc-k",
                    "hours": 4.0,
                    "issue_id": "385969",
                    "issue_key": None,
                    "started": report,
                }
            ]

    class FakeJira:
        def search_by_ids(self, ids, *, event_date=None, expand=None):
            assert "385969" in ids
            return [doc]

    import designops.pipelines.daily_worklogs as dw

    monkeypatch.setattr(dw, "TempoClient", lambda settings: FakeTempo())
    rows = _rows_from_tempo({"acc-k": object()}, report, FakeJira(), settings=None)
    assert len(rows) == 1
    assert rows[0]["hours"] == 4.0
    assert rows[0]["document"].raw["key"] == "UOM-512"


def test_time_log_bucket_excluded():
    assert not is_design_owned_ticket({"issue_type": "Time Logs", "original_hours": 1, "spent_hours": 40})
    assert is_design_owned_ticket({"issue_type": "Task", "original_hours": 6, "spent_hours": 4})
    assert not is_design_owned_ticket({"issue_type": "QA", "components": []})
    assert is_design_owned_ticket({"issue_type": "QA", "components": ["UX"]})


def test_zero_logged_range_label():
    report = date(2026, 8, 12)
    assert zero_logged_range_label("A", "P", report, hours_today=2.0) is None
    label = zero_logged_range_label(
        "A",
        "P",
        report,
        hours_today=0,
        prior_hours=[(date(2026, 8, 11), 0.0), (date(2026, 8, 10), 0.0)],
    )
    assert label == "10–12 Aug"


def test_jira_unavailable_drops_tickets_and_load_bars():
    digest = {
        "at_a_glance": {"active": 1, "need_you": 0, "repeating": 0, "no_report": 0},
        "status": [
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Minicart.",
                "next": "Checkout.",
            }
        ],
        "jira_available": False,
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(digest, date(2026, 8, 12), sample=True)
    assert "Jira unreachable" in html
    assert "logged ·" not in html
    assert "browse/" not in html


def test_person_card_order_and_repeat_chip():
    digest = {
        "at_a_glance": {"active": 2, "need_you": 1, "repeating": 1, "no_report": 0},
        "escalations": [
            {
                "text": "Maarja is waiting on your review",
                "who": "Maarja Truu",
                "project": "The Universal Group",
            }
        ],
        "status": [
            {
                "person": "Arturs Boroviks",
                "project": "Acer",
                "done": "Prep.",
                "next": "Client call.",
            },
            {
                "person": "Kirill Rogovets",
                "project": "University of Michigan",
                "done": "Minicart.",
                "next": "Finalise minicart.",
                "repeat": {
                    "clause": "Finalise minicart",
                    "day_list": "10, 11 and 12 Aug",
                    "streak": 3,
                    "tail": ". UOM-512 still In Progress.",
                    "text": "flag",
                    "shared": False,
                },
            },
        ],
        "leave_badges": {"Arturs Boroviks": "out 14 Aug"},
        "jira_available": True,
        "hours_by_person": {"Kirill Rogovets": 8.0, "Arturs Boroviks": 7.0},
        "needs_review": [],
        "open_questions": [],
        "todays_plans": [],
        "no_report": [],
    }
    html = render_digest(digest, date(2026, 8, 12), sample=True)
    assert html.index("Kirill Rogovets") < html.index("Arturs Boroviks")
    assert "Repeating ×3d" in html
    assert "Out 14 Aug" in html
    assert "8.0h" in html
    assert "repeating 3+ days" in html
    assert "By person" in html
    assert "display:grid" not in html and "display:flex" not in html
