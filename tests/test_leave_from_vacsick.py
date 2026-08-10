"""VACSICK leave detection — ≥8h/day Tempo worklogs → Person.on_leave."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

from designops.adapters.tempo import (
    _is_vacsick_worklog,
    normalize_tempo_worklog,
)
from designops.core.enums import PersonStatus
from designops.core.models import Person
from designops.pipelines.leave_from_vacsick import (
    LEAVE_DAY_HOURS_THRESHOLD,
    apply_leave_from_days,
    contiguous_leave_blocks,
    detect_leave_from_worklogs,
    hours_by_day,
    leave_days_from_hours,
    leave_scan_touches_horizon,
    leave_sync_max_until,
    leave_sync_step_targets,
    leave_sync_until,
    pick_leave_window,
    resolve_leave_sync_to,
    sync_leave_from_vacsick,
)
from designops.pipelines.weekly_availability import availability_marker


def _person(**kw):
    defaults = dict(
        id=uuid4(),
        full_name="Kirill Rogovets",
        jira_account_id="acct-kirill",
        status=PersonStatus.ACTIVE,
        leave_from=None,
        leave_until=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_normalize_tempo_worklog_and_vacsick_filter():
    row = {
        "author": {"accountId": "acct-1"},
        "startDate": "2026-08-04",
        "timeSpentSeconds": 28800,
        "description": "Paid vacation",
        "issue": {"id": 11881, "key": "VACSICK-159", "projectKey": "VACSICK"},
    }
    norm = normalize_tempo_worklog(row)
    assert norm is not None
    assert norm["account_id"] == "acct-1"
    assert norm["started"] == date(2026, 8, 4)
    assert norm["hours"] == 8.0
    assert norm["issue_id"] == "11881"
    assert _is_vacsick_worklog(row)
    assert not _is_vacsick_worklog(
        {"issue": {"key": "DES-1", "projectKey": "DES"}}
    )


def test_filter_vacsick_by_resolved_project_and_description():
    from designops.pipelines.leave_from_vacsick import filter_vacsick_worklogs

    logs = [
        {
            "account_id": "a",
            "started": date(2026, 8, 4),
            "hours": 8.0,
            "issue_id": "11881",
            "description": None,
        },
        {
            "account_id": "a",
            "started": date(2026, 8, 4),
            "hours": 1.0,
            "issue_id": "333531",
            "description": "client work",
        },
        {
            "account_id": "b",
            "started": date(2026, 8, 5),
            "hours": 8.0,
            "issue_id": None,
            "description": "Sick leave",
        },
    ]
    kept = filter_vacsick_worklogs(
        logs,
        issue_meta={
            "11881": {"key": "VACSICK-1", "project_key": "VACSICK"},
            "333531": {"key": "OMD-791", "project_key": "OMD"},
        },
    )
    assert len(kept) == 2
    assert kept[0]["issue_key"] == "VACSICK-1"
    assert kept[1]["description"] == "Sick leave"


def test_leave_days_threshold_8h():
    by_day = {
        date(2026, 8, 3): 4.0,
        date(2026, 8, 4): 8.0,
        date(2026, 8, 5): 8.5,
        date(2026, 8, 6): 7.9,
    }
    days = leave_days_from_hours(by_day, threshold=LEAVE_DAY_HOURS_THRESHOLD)
    assert days == [date(2026, 8, 4), date(2026, 8, 5)]


def test_hours_by_day_filters_account_and_window():
    logs = [
        {
            "account_id": "acct-kirill",
            "started": date(2026, 8, 4),
            "hours": 8.0,
        },
        {
            "account_id": "acct-kirill",
            "started": date(2026, 8, 4),
            "hours": 0.0,  # ignored in sum still adds 0
        },
        {
            "account_id": "other",
            "started": date(2026, 8, 4),
            "hours": 8.0,
        },
        {
            "account_id": "acct-kirill",
            "started": date(2026, 7, 28),  # outside window
            "hours": 8.0,
        },
    ]
    by_day = hours_by_day(
        logs,
        account_id="acct-kirill",
        from_date=date(2026, 8, 3),
        to_date=date(2026, 8, 7),
    )
    assert by_day == {date(2026, 8, 4): 8.0}


def test_contiguous_leave_blocks_and_pick_window():
    days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 14)]
    assert contiguous_leave_blocks(days) == [
        (date(2026, 8, 3), date(2026, 8, 5)),
        (date(2026, 8, 14), date(2026, 8, 14)),
    ]
    window, on_today = pick_leave_window(contiguous_leave_blocks(days), date(2026, 8, 4))
    assert window == (date(2026, 8, 3), date(2026, 8, 5))
    assert on_today is True
    window, on_today = pick_leave_window(contiguous_leave_blocks(days), date(2026, 8, 7))
    assert window == (date(2026, 8, 14), date(2026, 8, 14))
    assert on_today is False


def test_contiguous_leave_bridges_weekend_only():
    """Tempo skips weekends — Fri + Mon VACSICK is one vacation, not two blocks."""
    days = [
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
        date(2026, 8, 10),
        date(2026, 8, 11),
        date(2026, 8, 12),
        date(2026, 8, 13),
        date(2026, 8, 14),
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
    ]
    assert contiguous_leave_blocks(days) == [
        (date(2026, 8, 5), date(2026, 8, 19)),
    ]
    window, on_today = pick_leave_window(contiguous_leave_blocks(days), date(2026, 8, 4))
    assert window == (date(2026, 8, 5), date(2026, 8, 19))
    assert on_today is False


def test_apply_leave_sets_status_and_leave_until():
    p = _person()
    ref = date(2026, 8, 4)
    # Weekday gap (Tue leave + Thu leave): only the block containing reference_date
    assert apply_leave_from_days(p, [date(2026, 8, 4), date(2026, 8, 6)], reference_date=ref) is True
    assert p.status == PersonStatus.ON_LEAVE
    assert p.leave_from == date(2026, 8, 4)
    assert p.leave_until == date(2026, 8, 4)
    # Reference moves into the later block
    assert apply_leave_from_days(p, [date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 7)], reference_date=date(2026, 8, 7)) is True
    assert p.leave_from == date(2026, 8, 6)
    assert p.leave_until == date(2026, 8, 7)
    # Empty days — no clear
    assert apply_leave_from_days(p, []) is False
    assert p.status == PersonStatus.ON_LEAVE


def test_apply_leave_skips_out_people():
    p = _person(status=PersonStatus.OUT)
    assert apply_leave_from_days(p, [date(2026, 8, 4)]) is False
    assert p.status == PersonStatus.OUT


def test_detect_leave_and_availability_out_partial():
    mon = date(2026, 8, 3)  # Monday
    fri = date(2026, 8, 7)
    kirill = _person()
    arturs = _person(
        id=uuid4(),
        full_name="Arturs Boroviks",
        jira_account_id="acct-arturs",
    )
    worklogs = [
        {
            "account_id": "acct-kirill",
            "started": date(2026, 8, 3),
            "hours": 8.0,
            "issue_key": "VACSICK-200",
        },
        {
            "account_id": "acct-kirill",
            "started": date(2026, 8, 4),
            "hours": 8.0,
            "issue_key": "VACSICK-200",
        },
        {
            "account_id": "acct-kirill",
            "started": date(2026, 8, 5),
            "hours": 8.0,
            "issue_key": "VACSICK-200",
        },
        # Arturs only 4h — not leave
        {
            "account_id": "acct-arturs",
            "started": date(2026, 8, 4),
            "hours": 4.0,
            "issue_key": "VACSICK-200",
        },
    ]
    dets = detect_leave_from_worklogs(
        [kirill, arturs], worklogs, week_monday=mon, week_friday=fri, reference_date=mon
    )
    assert len(dets) == 1
    assert dets[0].full_name == "Kirill Rogovets"
    assert dets[0].leave_until == date(2026, 8, 5)
    assert dets[0].leave_from == date(2026, 8, 3)
    assert dets[0].updated is True
    assert kirill.status == PersonStatus.ON_LEAVE
    assert kirill.leave_from == date(2026, 8, 3)
    assert arturs.status == PersonStatus.ACTIVE

    # Mid-week end (leave_until Wed) → PARTIAL without leave_from
    assert availability_marker(kirill.status, date(2026, 8, 5), mon) == "PARTIAL"

    # Whole week Mon–Fri → OUT
    assert (
        availability_marker(
            kirill.status,
            fri,
            mon,
            leave_from=date(2026, 8, 3),
        )
        == "OUT"
    )

    # Mid-week start Wed–Fri → PARTIAL (Dorota case)
    assert (
        availability_marker(
            "on_leave",
            fri,
            mon,
            leave_from=date(2026, 8, 5),
        )
        == "PARTIAL"
    )


def test_detect_leave_spans_multiple_weeks():
    """Weekday-only VACSICK across weekends → one block through last leave day."""
    from uuid import uuid4

    dorota = Person(
        id=uuid4(),
        full_name="Dorota Umiastowska",
        emails=["dorota@example.com"],
        jira_account_id="acct-dorota",
        status=PersonStatus.ACTIVE,
        leave_from=None,
        leave_until=None,
    )
    # Real Tempo shape: no weekend rows
    worklogs = [
        {
            "account_id": "acct-dorota",
            "started": date(2026, 8, d),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        }
        for d in (5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 19)
    ]
    dets = detect_leave_from_worklogs(
        [dorota],
        worklogs,
        week_monday=date(2026, 8, 3),
        week_friday=date(2026, 8, 30),
        reference_date=date(2026, 8, 4),
    )
    assert len(dets) == 1
    assert dets[0].leave_from == date(2026, 8, 5)
    assert dets[0].leave_until == date(2026, 8, 19)
    assert dorota.leave_until == date(2026, 8, 19)


def test_predrag_sparse_vacsick_does_not_bridge_gap():
    """Single sick day + later sick day must not show as one Mon–Fri vacation."""
    predrag = _person(
        full_name="Predrag Gavrilovikj",
        jira_account_id="acct-predrag",
    )
    worklogs = [
        {
            "account_id": "acct-predrag",
            "started": date(2026, 8, 3),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        },
        {
            "account_id": "acct-predrag",
            "started": date(2026, 8, 14),
            "hours": 8.0,
            "issue_key": "VACSICK-2",
        },
    ]
    dets = detect_leave_from_worklogs(
        [predrag],
        worklogs,
        week_monday=date(2026, 8, 3),
        week_friday=date(2026, 8, 30),
        reference_date=date(2026, 8, 4),
    )
    assert len(dets) == 1
    assert dets[0].leave_from == date(2026, 8, 14)
    assert dets[0].leave_until == date(2026, 8, 14)
    assert predrag.leave_from == date(2026, 8, 14)
    assert predrag.leave_until == date(2026, 8, 14)
    from designops.core.identity import effective_status

    assert (
        effective_status(
            predrag.status,
            predrag.leave_until,
            date(2026, 8, 4),
            leave_from=predrag.leave_from,
        )
        == PersonStatus.ACTIVE
    )


def test_leave_sync_until_extends_past_report_week():
    assert leave_sync_until(date(2026, 8, 3), date(2026, 8, 7)) == date(2026, 8, 30)


def test_leave_sync_max_until_two_months():
    mon = date(2026, 8, 3)
    assert leave_sync_max_until(mon) == date(2026, 10, 1)
    assert leave_sync_until(mon, mon + timedelta(days=4)) <= leave_sync_max_until(mon)


def test_leave_scan_touches_horizon():
    sync_to = date(2026, 8, 30)  # Sun — last working day Fri Aug 28
    assert leave_scan_touches_horizon([date(2026, 8, 28)], sync_to) is True
    assert leave_scan_touches_horizon([date(2026, 8, 27)], sync_to) is False
    assert leave_scan_touches_horizon([], sync_to) is False


def test_leave_sync_step_targets():
    mon = date(2026, 8, 3)
    targets = leave_sync_step_targets(mon, date(2026, 8, 7))
    assert targets == [
        date(2026, 8, 30),
        date(2026, 9, 6),
        date(2026, 9, 13),
        leave_sync_max_until(mon),
    ]


def test_resolve_leave_sync_extends_when_vacation_fills_horizon():
    """Vlad-like: one +1w step is enough when leave ends before the next horizon."""
    mon = date(2026, 8, 3)
    vlad = _person(
        full_name="Vlad Shemetovets",
        jira_account_id="acct-vlad",
    )
    initial_to = leave_sync_until(mon, date(2026, 8, 7))
    full_logs = [
        {
            "account_id": "acct-vlad",
            "started": date(2026, 8, d),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        }
        for d in (14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28)
    ] + [
        {
            "account_id": "acct-vlad",
            "started": date(2026, 8, 31),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        },
        {
            "account_id": "acct-vlad",
            "started": date(2026, 9, 1),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        },
        {
            "account_id": "acct-vlad",
            "started": date(2026, 9, 2),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        },
    ]
    assert resolve_leave_sync_to(
        [vlad], full_logs, week_monday=mon, week_friday=date(2026, 8, 7)
    ) == date(2026, 9, 6)

    # Leave ends before horizon — no extension
    short_logs = full_logs[:5]
    assert resolve_leave_sync_to(
        [vlad], short_logs, week_monday=mon, week_friday=date(2026, 8, 7)
    ) == initial_to


def test_resolve_leave_sync_steps_to_two_months():
    """After two +1w steps still touching horizon → jump to 2-month cap."""
    mon = date(2026, 8, 3)
    person = _person(jira_account_id="acct-long")
    # Leave every weekday from Aug 14 through Sep 11 (fills each weekly horizon)
    days = []
    d = date(2026, 8, 14)
    end = date(2026, 9, 11)
    while d <= end:
        if d.weekday() < 5:
            days.append(
                {
                    "account_id": "acct-long",
                    "started": d,
                    "hours": 8.0,
                    "issue_key": "VACSICK-1",
                }
            )
        d += timedelta(days=1)
    assert resolve_leave_sync_to(
        [person], days, week_monday=mon, week_friday=date(2026, 8, 7)
    ) == leave_sync_max_until(mon)


def test_detect_leave_vlad_extended_vacation():
    """Multi-week vacation past initial 4-week horizon is captured when scan extends."""
    mon = date(2026, 8, 3)
    sync_to = leave_sync_max_until(mon)
    vlad = _person(
        full_name="Vlad Shemetovets",
        jira_account_id="acct-vlad",
    )
    worklogs = [
        {
            "account_id": "acct-vlad",
            "started": date(2026, 8, d),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        }
        for d in (14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28)
    ] + [
        {
            "account_id": "acct-vlad",
            "started": date(2026, 9, d),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        }
        for d in (1, 2)
    ] + [
        {
            "account_id": "acct-vlad",
            "started": date(2026, 8, 31),
            "hours": 8.0,
            "issue_key": "VACSICK-1",
        }
    ]
    dets = detect_leave_from_worklogs(
        [vlad],
        worklogs,
        week_monday=mon,
        week_friday=sync_to,
        reference_date=date(2026, 8, 7),
    )
    assert len(dets) == 1
    assert dets[0].leave_from == date(2026, 8, 14)
    assert dets[0].leave_until == date(2026, 9, 2)
    assert vlad.leave_until == date(2026, 9, 2)


def test_sync_leave_noop_without_tempo_token(monkeypatch):
    from designops.core.config import Settings

    s = Settings(
        TEMPO_API_TOKEN="",
        DATABASE_URL="postgresql+psycopg://x:x@localhost/x",
    )
    people = [_person()]
    cov = sync_leave_from_vacsick(
        people,
        week_monday=date(2026, 8, 3),
        settings=s,
    )
    assert cov["configured"] is False
    assert people[0].status == PersonStatus.ACTIVE
    assert "TEMPO_API_TOKEN" in (cov.get("note") or "")


def test_sync_leave_uses_client(monkeypatch):
    from designops.core.config import Settings

    class FakeTempo:
        def __init__(self):
            self.calls: list[tuple[date, date]] = []

        def list_worklogs(self, **kwargs):
            to_d = kwargs["to_date"]
            self.calls.append((kwargs["from_date"], to_d))
            if to_d == date(2026, 8, 30):
                days = [14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28]
            else:
                days = [14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31]
            logs = [
                {
                    "account_id": "acct-vlad",
                    "started": date(2026, 8, d),
                    "hours": 8.0,
                    "issue_key": "VACSICK-1",
                }
                for d in days
            ]
            if to_d >= date(2026, 9, 6):
                logs.extend(
                    [
                        {
                            "account_id": "acct-vlad",
                            "started": date(2026, 9, d),
                            "hours": 8.0,
                            "issue_key": "VACSICK-1",
                        }
                        for d in (1, 2)
                    ]
                )
            return logs

    s = Settings(
        TEMPO_API_TOKEN="tok",
        DATABASE_URL="postgresql+psycopg://x:x@localhost/x",
    )
    p = _person(jira_account_id="acct-vlad", full_name="Vlad Shemetovets")
    fake = FakeTempo()
    cov = sync_leave_from_vacsick(
        [p],
        week_monday=date(2026, 8, 3),
        week_friday=date(2026, 8, 7),
        settings=s,
        client=fake,
    )
    assert cov["configured"] is True
    assert cov["sync_extended"] is True
    assert cov["sync_until"] == date(2026, 9, 6).isoformat()
    assert len(fake.calls) == 2
    assert fake.calls[1][1] == date(2026, 9, 6)
    assert p.leave_until == date(2026, 9, 2)


def test_sync_leave_uses_client_short_vacation(monkeypatch):
    from designops.core.config import Settings

    class FakeTempo:
        def list_worklogs(self, **kwargs):
            return [
                {
                    "account_id": "acct-kirill",
                    "started": date(2026, 8, 4),
                    "hours": 8.0,
                    "issue_key": "VACSICK-1",
                }
            ]

    s = Settings(
        TEMPO_API_TOKEN="tok",
        DATABASE_URL="postgresql+psycopg://x:x@localhost/x",
    )
    p = _person()
    cov = sync_leave_from_vacsick(
        [p],
        week_monday=date(2026, 8, 3),
        week_friday=date(2026, 8, 7),
        settings=s,
        client=FakeTempo(),
    )
    assert cov["configured"] is True
    assert cov["fetched"] == 1
    assert cov.get("sync_extended") is False
    assert cov["updated_names"] == ["Kirill Rogovets"]
    assert p.status == PersonStatus.ON_LEAVE
    assert p.leave_until == date(2026, 8, 4)
