"""Unit tests for A3 availability markers, load flags, and planning-board math."""

from __future__ import annotations

from datetime import date

import pytest

from designops.adapters.jira import (
    issue_to_document,
    remaining_hours,
    remaining_seconds_from_issue,
)
from designops.pipelines.weekly_availability import (
    at_a_glance_kpis,
    availability_marker,
    board_sort_key,
    booked_hours_from_docs,
    build_person_board_row,
    burn_pct,
    burn_pct_class,
    classify_capacity_band,
    extract_ticket_keys,
    group_tickets_by_status,
    is_hardware_ticket,
    load_flags,
    planned_and_blocked,
    previous_friday,
    resolve_week_monday,
    week_monday_on_or_before,
)
from designops.pipelines.render import render_weekly_backlog


def test_week_date_helpers():
    assert week_monday_on_or_before(date(2026, 7, 24)) == date(2026, 7, 20)  # Fri → Mon
    assert week_monday_on_or_before(date(2026, 7, 20)) == date(2026, 7, 20)
    assert previous_friday(date(2026, 7, 27)) == date(2026, 7, 24)
    assert resolve_week_monday(date(2026, 7, 26)) == date(2026, 7, 27)  # Sun → next Mon
    assert resolve_week_monday(date(2026, 7, 25)) == date(2026, 7, 27)  # Sat → next Mon
    assert resolve_week_monday(date(2026, 7, 27)) == date(2026, 7, 27)  # Mon stays
    assert resolve_week_monday(date(2026, 7, 22)) == date(2026, 7, 20)  # Wed → that week


def test_leave_overlap_partial_days():
    from designops.pipelines.weekly_availability import leave_overlap_in_week

    mon = date(2026, 8, 3)
    info = leave_overlap_in_week(
        week_monday=mon,
        leave_from=date(2026, 8, 5),
        leave_until=date(2026, 8, 7),
    )
    assert info is not None
    assert info["leave_days"] == 3
    assert info["leave_hours"] == 24.0
    assert "Wed" in info["leave_range"] and "Fri" in info["leave_range"]

    row = build_person_board_row(
        name="Dorota Umiastowska",
        availability="PARTIAL",
        tickets=[],
        capacity=40,
        has_friday_plan=False,
        week_monday=mon,
        leave_from=date(2026, 8, 5),
        leave_until=date(2026, 8, 7),
    )
    assert row["leave_days"] == 3
    assert row["leave_hours"] == 24.0
    assert "3d off" in row["avail_label"]
    assert "24h" in row["flag"]
    assert row["flagline"]["lab"] == "Partial leave"


def test_availability_out_and_leave():
    mon = date(2026, 7, 27)
    assert availability_marker("out", None, mon) == "OUT"
    assert availability_marker("on_leave", None, mon) == "OUT"
    assert availability_marker("on_leave", date(2026, 7, 29), mon) == "PARTIAL"
    assert availability_marker("on_leave", date(2026, 8, 7), mon) == "OUT"
    assert availability_marker("active", None, mon) == "AVAILABLE"
    assert availability_marker("on_leave", date(2026, 7, 20), mon) == "AVAILABLE"
    # Mid-week start (Wed–Fri) must be PARTIAL, not full-week OUT
    assert (
        availability_marker(
            "on_leave",
            date(2026, 7, 31),
            mon,
            leave_from=date(2026, 7, 29),
        )
        == "PARTIAL"
    )
    # Whole week Mon–Fri → OUT
    assert (
        availability_marker(
            "on_leave",
            date(2026, 7, 31),
            mon,
            leave_from=date(2026, 7, 27),
        )
        == "OUT"
    )


def test_load_flags():
    assert load_flags(
        booked_hours=45, normal_hours=40, availability="AVAILABLE", has_friday_plan=True
    ) == (True, False)
    assert load_flags(
        booked_hours=0, normal_hours=40, availability="AVAILABLE", has_friday_plan=False
    ) == (False, True)
    assert load_flags(
        booked_hours=0, normal_hours=40, availability="OUT", has_friday_plan=False
    ) == (False, False)
    assert load_flags(
        booked_hours=10, normal_hours=40, availability="AVAILABLE", has_friday_plan=False
    ) == (False, False)


def test_extract_ticket_keys_and_ranges():
    text = "Plan: SCOP1-37 through SCOP1-39 plus WIEND-108 and DES-1 to DES-3"
    keys = extract_ticket_keys(text)
    assert "SCOP1-37" in keys
    assert "SCOP1-38" in keys
    assert "SCOP1-39" in keys
    assert "WIEND-108" in keys
    assert "DES-1" in keys
    assert "DES-2" in keys
    assert "DES-3" in keys
    assert extract_ticket_keys("SCOP1-37→42") == [
        f"SCOP1-{n}" for n in range(37, 43)
    ]


def test_burn_pct_bands():
    assert burn_pct(10, 0) is None
    assert burn_pct(None, 5) is None
    assert burn_pct(10, 10) == 100
    assert burn_pct_class(100) == "over"
    assert burn_pct_class(85) == "hi"
    assert burn_pct_class(65) == "mid"
    assert burn_pct_class(50) == "lo"
    assert burn_pct_class(20) == "ok"
    assert burn_pct_class(None) == "mut"


def test_capacity_bands():
    out = classify_capacity_band(
        planned_hours=0, blocked_hours=0, capacity=40, availability="OUT"
    )
    assert out["band"] == "OUT" and out["flag"] == "Out"

    idle_blocked = classify_capacity_band(
        planned_hours=0, blocked_hours=36, capacity=40, availability="AVAILABLE"
    )
    assert idle_blocked["band"] == "IDLE"
    assert idle_blocked["flag"] == "Blocked → idle"

    spare = classify_capacity_band(
        planned_hours=18, blocked_hours=0, capacity=40, availability="AVAILABLE"
    )
    assert spare["band"] == "SPARE"
    assert "Spare" in spare["flag"]
    assert spare["bar_fill"] == "idle"
    assert spare["bar_pct"] == 45  # 18h of 40h — proportional fill

    mid = classify_capacity_band(
        planned_hours=25, blocked_hours=0, capacity=40, availability="AVAILABLE"
    )
    assert mid["bar_pct"] == 62  # 25h of 40h — just over half the track

    at_cap = classify_capacity_band(
        planned_hours=35, blocked_hours=0, capacity=40, availability="AVAILABLE"
    )
    assert at_cap["band"] == "AT_CAPACITY"
    assert at_cap["bar_fill"] == "bal"

    over = classify_capacity_band(
        planned_hours=66, blocked_hours=0, capacity=40, availability="AVAILABLE"
    )
    assert over["band"] == "OVER_PLANNED"
    assert over["flag"] == "Over-planned"  # no magnitude
    assert over["bar_fill"] == "over"
    assert over["bar_pct"] == 100

    hot = classify_capacity_band(
        planned_hours=180, blocked_hours=0, capacity=40, availability="AVAILABLE"
    )
    assert hot["bar_fill"] == "hot"


def test_planned_excludes_blocked_and_hardware():
    tickets = [
        {"key": "A-1", "status": "In Progress", "remaining_hours": 10, "project_key": "DES", "issue_type": "Task"},
        {"key": "A-2", "status": "ON HOLD", "remaining_hours": 36, "project_key": "DES", "issue_type": "Task"},
        {"key": "A-3", "status": "Client Action", "remaining_hours": 8, "project_key": "DES", "issue_type": "Task"},
        {"key": "A-4", "status": "Backlog", "remaining_hours": 20, "project_key": "DES", "issue_type": "Task"},
        {"key": "SEEUX-25", "status": "To Do", "remaining_hours": 40, "project_key": "SEEUX", "issue_type": "Epic"},
        {"key": "IMR-9", "status": "In Progress", "remaining_hours": 99, "project_key": "IMR", "issue_type": "In Use"},
    ]
    assert is_hardware_ticket(tickets[5])
    from designops.pipelines.weekly_availability import filter_work_tickets, is_epic_ticket

    assert is_epic_ticket(tickets[4])
    work = filter_work_tickets(tickets)
    assert all(t["key"] != "SEEUX-25" for t in work)
    planned, blocked = planned_and_blocked(work)
    # Only In Progress / To Do count; Client Action + Backlog do not; Epic dropped
    assert planned == 10
    assert blocked == 36
    planned_ded, _ = planned_and_blocked(work, dedicated_weekly_hours=20)
    assert planned_ded == 30


def test_group_tickets_blocked_last():
    tickets = [
        {"key": "B-1", "status": "ON HOLD", "remaining_hours": 5, "original_hours": 5, "spent_hours": None},
        {"key": "A-1", "status": "In Progress", "remaining_hours": 3, "original_hours": 8, "spent_hours": 5},
        {"key": "C-1", "status": "To Do", "remaining_hours": 2, "original_hours": 2, "spent_hours": None},
    ]
    groups = group_tickets_by_status(tickets)
    labels = [g["status"] for g in groups]
    assert labels[0] == "In Progress"
    assert labels[-1] == "Blocked"
    assert groups[-1]["blocked"] is True


def test_build_person_and_kpis():
    tickets = [
        {
            "key": "X-1",
            "summary": "Work",
            "status": "In Progress",
            "original_hours": 20,
            "spent_hours": 5,
            "remaining_hours": 15,
            "project_key": "X",
            "issue_type": "Task",
            "url": "https://example.atlassian.net/browse/X-1",
        },
        {
            "key": "X-2",
            "summary": "Hold",
            "status": "ON HOLD",
            "original_hours": 10,
            "spent_hours": None,
            "remaining_hours": 10,
            "project_key": "X",
            "issue_type": "Task",
        },
        {
            "key": "X-3",
            "summary": "Waiting on client",
            "status": "Client Action",
            "original_hours": 6,
            "spent_hours": 1,
            "remaining_hours": 5,
            "project_key": "X",
            "issue_type": "Task",
        },
        {
            "key": "X-4",
            "summary": "Later",
            "status": "Backlog",
            "original_hours": 12,
            "spent_hours": 2,
            "remaining_hours": 10,
            "project_key": "X",
            "issue_type": "Task",
        },
    ]
    row = build_person_board_row(
        name="Vlad Shemetovets",
        availability="AVAILABLE",
        tickets=tickets,
        capacity=40,
        has_friday_plan=True,
        friday_keys=["X-1", "X-2"],
        is_dedicated=True,
        dedicated_weekly_hours=40,
    )
    # 15h IP + 40h dedicated; Client Action / Backlog / Hold not in planned
    assert row["planned_hours"] == 55
    assert row["blocked_hours"] == 10
    assert row["band"] == "OVER_PLANNED"
    assert row["dedicated_weekly_hours"] == 40
    assert not row["unverified"]
    detail_labels = [g["status"] for g in row["status_groups"]]
    assert detail_labels == ["In Progress", "Client Action"]
    assert row["other_summary"]["count"] == 2  # ON HOLD + Backlog
    assert row["other_summary"]["est_hours"] == 22
    assert row["other_summary"]["log_hours"] == 2
    assert set(row["other_summary"]["keys"]) == {"X-2", "X-4"}
    assert row["other_summary"]["jira_url"]
    assert "key%20in" in row["other_summary"]["jira_url"] or "key in" in row["other_summary"]["jira_url"]
    assert "X-2" in row["other_summary"]["jira_url"]

    no_friday = build_person_board_row(
        name="Tamari Giunashvili",
        availability="AVAILABLE",
        tickets=tickets,
        capacity=40,
        has_friday_plan=False,
        is_dedicated=True,
        dedicated_weekly_hours=20,
    )
    assert no_friday["planned_hours"] == 35  # 15 + 20 dedicated
    assert not no_friday["unverified"]

    people = [
        row,
        no_friday,
        build_person_board_row(
            name="Agnese",
            availability="OUT",
            tickets=[],
            capacity=40,
            has_friday_plan=False,
        ),
        build_person_board_row(
            name="Kirill",
            availability="AVAILABLE",
            tickets=[
                {
                    "key": "K-1",
                    "status": "In Progress",
                    "remaining_hours": 180,
                    "original_hours": 180,
                    "spent_hours": None,
                    "project_key": "K",
                    "issue_type": "Task",
                }
            ],
            capacity=40,
            has_friday_plan=True,
            friday_keys=["K-1"],
        ),
    ]
    glance = at_a_glance_kpis(people, capacity=40)
    assert glance["fully_booked"] == 2  # Vlad + Kirill
    assert glance["spare_capacity"] == 0  # Tamari at 35 ≥ SPARE_THRESHOLD 30 → AT_CAPACITY
    assert glance["have_blocked"] == 2  # Vlad + Tamari
    assert glance["on_leave"] == 1
    people.sort(key=board_sort_key)
    assert people[0]["name"] == "Kirill"  # OVER first by hours


def test_friday_email_without_jira_keys_is_not_no_report():
    """A Friday daily/plan email counts even if it names no ticket keys."""
    from types import SimpleNamespace
    from uuid import uuid4

    from designops.adapters.documents import Document
    from designops.pipelines.weekly_backlog import _build_person_rows

    pid = uuid4()
    person = SimpleNamespace(
        id=pid, full_name="Arturs Boroviks", status="active", leave_until=None
    )
    friday_doc = Document(
        source="fairwind",
        external_id="e1",
        event_date=date(2026, 7, 24),
        author_identity="arturs@scandiweb.com",
        title="Re: July Reports",
        body="*Friday Report (July 24)*\n*Acer*\n- finishing PLP pages",
    )
    rows = _build_person_rows(
        [person],
        date(2026, 7, 27),
        {pid: [friday_doc]},
        {pid: []},
        {},
        40.0,
    )
    assert len(rows) == 1
    assert rows[0]["unverified"] is False
    assert rows[0]["friday_excerpt"]
    assert (rows[0].get("flagline") or {}).get("lab") != "Verify"


def test_no_friday_email_not_flagged():
    """Missing Friday report should NOT flag unverified anymore."""
    from types import SimpleNamespace
    from uuid import uuid4

    from designops.pipelines.weekly_backlog import _build_person_rows

    pid = uuid4()
    person = SimpleNamespace(
        id=pid, full_name="Vlad Shemetovets", status="active", leave_until=None
    )
    rows = _build_person_rows(
        [person],
        date(2026, 7, 27),
        {pid: []},
        {pid: []},
        {},
        40.0,
    )
    assert rows[0]["unverified"] is False
    assert (rows[0].get("flagline") or {}).get("lab") != "Verify"


def test_narrative_matches_ticket_before_project():
    """Plan text without keys: ticket summary first, then project name."""
    from uuid import uuid4

    from designops.core.registry import ProjectEntry, ProjectRegistry
    from designops.pipelines.weekly_backlog import _select_planned_tickets

    assigned = [
        {
            "key": "ACERP1-10",
            "summary": "PLP wireframes for search",
            "status": "In Progress",
            "remaining_hours": 8,
            "original_hours": 8,
            "spent_hours": 0,
            "project_key": "ACERP1",
            "issue_type": "Task",
        },
        {
            "key": "ACERP1-99",
            "summary": "Unrelated backlog ticket",
            "status": "To Do",
            "remaining_hours": 40,
            "original_hours": 40,
            "spent_hours": 0,
            "project_key": "ACERP1",
            "issue_type": "Task",
        },
        {
            "key": "UOM-1",
            "summary": "Homepage adjustments",
            "status": "To Do",
            "remaining_hours": 12,
            "original_hours": 12,
            "spent_hours": 0,
            "project_key": "UOM",
            "issue_type": "Task",
        },
    ]
    registry = ProjectRegistry(
        [
            (
                ProjectEntry(
                    id=uuid4(),
                    canonical_name="Acer",
                    jira_project_key="ACERP1",
                    fairwind_account_id=None,
                ),
                ["Acer"],
            ),
            (
                ProjectEntry(
                    id=uuid4(),
                    canonical_name="University of Michigan",
                    jira_project_key="UOM",
                    fairwind_account_id=None,
                ),
                ["University of Michigan", "UMich"],
            ),
        ]
    )

    # All active assigned tickets are always returned regardless of friday text
    tickets, keys, no_plan = _select_planned_tickets(
        friday_text="Friday plan — finishing PLP wireframes for search on Acer",
        assigned_tickets=assigned,
        all_by_key={},
        availability="AVAILABLE",
        registry=registry,
    )
    assert no_plan is False
    result_keys = {t["key"] for t in tickets}
    assert result_keys == {"ACERP1-10", "ACERP1-99", "UOM-1"}

    tickets2, _, _ = _select_planned_tickets(
        friday_text="Friday Report\n*Acer*\n- continue discovery work",
        assigned_tickets=assigned,
        all_by_key={},
        availability="AVAILABLE",
        registry=registry,
    )
    assert {t["key"] for t in tickets2} == {"ACERP1-10", "ACERP1-99", "UOM-1"}


def test_remaining_seconds_and_hours():
    assert remaining_seconds_from_issue(
        {"timetracking": {"remainingEstimateSeconds": 7200}}
    ) == 7200
    assert remaining_seconds_from_issue({"timeestimate": 3600}) == 3600
    assert remaining_hours(7200) == 2.0
    assert remaining_hours(None) == 0.0


def test_issue_to_document_and_booked_hours(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    from designops.core.config import get_settings

    get_settings.cache_clear()
    issue = {
        "key": "DES-12",
        "fields": {
            "summary": "Redraw PLP",
            "status": {"name": "In Progress"},
            "assignee": {
                "accountId": "abc123",
                "emailAddress": "arturs.boroviks@scandiweb.com",
            },
            "project": {"key": "DES"},
            "issuetype": {"name": "Task"},
            "timetracking": {
                "originalEstimateSeconds": 28800,
                "timeSpentSeconds": 7200,
                "remainingEstimateSeconds": 14400,
            },
            "duedate": "2026-07-30",
        },
    }
    doc = issue_to_document(issue, event_date=date(2026, 7, 27))
    assert doc.source == "jira"
    assert doc.external_id == "DES-12"
    assert doc.author_identity == "abc123"
    assert doc.raw["status"] == "In Progress"
    assert doc.raw["original_hours"] == 8.0
    assert doc.raw["spent_hours"] == 2.0
    assert doc.raw["remaining_hours"] == 4.0
    assert booked_hours_from_docs([doc]) == 4.0
    get_settings.cache_clear()


def test_timetracking_field_fallbacks():
    from designops.adapters.jira import (
        original_seconds_from_issue,
        spent_seconds_from_issue,
    )

    fields = {
        "timeoriginalestimate": 3600,
        "timespent": 1800,
        "timeestimate": 900,
    }
    assert original_seconds_from_issue(fields) == 3600
    assert spent_seconds_from_issue(fields) == 1800
    assert remaining_seconds_from_issue(fields) == 900


def test_agnese_out_in_merge_shape():
    mon = date(2026, 7, 27)
    assert availability_marker("out", None, mon) == "OUT"
    overload, idle = load_flags(
        booked_hours=0, normal_hours=40, availability="OUT", has_friday_plan=False
    )
    assert not overload and not idle


def test_dedicated_only_person_is_at_capacity():
    """Dedicated hours alone count as weekly workload with no Jira tickets."""
    row = build_person_board_row(
        name="Predrag Gavrilovikj",
        availability="AVAILABLE",
        tickets=[],
        capacity=40,
        has_friday_plan=False,
        is_dedicated=True,
        dedicated_weekly_hours=40,
    )
    assert row["planned_hours"] == 40
    assert row["band"] == "AT_CAPACITY"
    assert row["status_groups"] == []
    assert row["other_summary"] is None
    assert row["dedicated_weekly_hours"] == 40


def test_render_planning_board_html():
    people = [
        build_person_board_row(
            name="Vlad Shemetovets",
            availability="AVAILABLE",
            tickets=[
                {
                    "key": "SID-292",
                    "summary": "Legeakademie ppt portfolio",
                    "status": "To Do",
                    "original_hours": 16,
                    "spent_hours": None,
                    "remaining_hours": 16,
                    "project_key": "SID",
                    "issue_type": "Task",
                    "url": "https://example.atlassian.net/browse/SID-292",
                },
                {
                    "key": "SID-300",
                    "summary": "Waiting",
                    "status": "Client Action",
                    "original_hours": 4,
                    "spent_hours": 1,
                    "remaining_hours": 3,
                    "project_key": "SID",
                    "issue_type": "Task",
                    "url": "https://example.atlassian.net/browse/SID-300",
                },
                {
                    "key": "SID-301",
                    "summary": "Later idea",
                    "status": "Backlog",
                    "original_hours": 8,
                    "spent_hours": 0,
                    "remaining_hours": 8,
                    "project_key": "SID",
                    "issue_type": "Task",
                },
            ],
            capacity=40,
            has_friday_plan=True,
            friday_keys=["SID-292"],
            is_dedicated=True,
            dedicated_weekly_hours=40,
        ),
        build_person_board_row(
            name="Agnese Čākure",
            availability="OUT",
            tickets=[],
            capacity=40,
            has_friday_plan=False,
        ),
    ]
    people[0]["flagline"] = {
        "kind": "over",
        "lab": "Overloaded",
        "text": "Over-planned at 56h. Heaviest on dedicated Northerner + SID To Do.",
    }
    digest = {
        "week_of": "2026-07-20",
        "friday_date": "2026-07-17",
        "capacity": 40,
        "at_a_glance": at_a_glance_kpis(people, capacity=40),
        "rebalance": {
            "title": "Before Monday — 1 moves",
            "subtitle": "Route overflow to spare capacity.",
            "moves": [{"text": "Route ~22h to Vlad.", "project": "capacity"}],
        },
        "people": people,
    }
    html = render_weekly_backlog(
        digest, date(2026, 7, 20), date(2026, 7, 17), sample=True, coverage={}
    )
    assert "Weekly Planning Board" in html or "planned week" in html.lower() or "Planned week" in html
    assert "fully booked" in html
    assert "Capacity board" in html
    assert "Vlad Shemetovets" in html
    assert "SID-292" in html
    assert "SID-300" in html  # Client Action listed in detail
    assert "Other assigned" in html
    assert "Open in Jira" in html
    assert "Dedicated" in html
    assert "Where to rebalance" in html
    assert "Overloaded" in html


@pytest.mark.jira
def test_live_jira_myself():
    from designops.adapters.jira import JiraClient
    from designops.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    if not s.jira_configured:
        pytest.skip("JIRA_* not set")
    me = JiraClient(s).myself()
    assert me.get("accountId")
