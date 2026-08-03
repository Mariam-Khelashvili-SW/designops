"""VACSICK leave detection — ≥8h/day Tempo worklogs → Person.on_leave."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from designops.adapters.tempo import (
    _is_vacsick_worklog,
    normalize_tempo_worklog,
)
from designops.core.enums import PersonStatus
from designops.pipelines.leave_from_vacsick import (
    LEAVE_DAY_HOURS_THRESHOLD,
    apply_leave_from_days,
    detect_leave_from_worklogs,
    hours_by_day,
    leave_days_from_hours,
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


def test_apply_leave_sets_status_and_leave_until():
    p = _person()
    assert apply_leave_from_days(p, [date(2026, 8, 4), date(2026, 8, 6)]) is True
    assert p.status == PersonStatus.ON_LEAVE
    assert p.leave_from == date(2026, 8, 4)
    assert p.leave_until == date(2026, 8, 6)
    # Extends later leave_until; keeps earlier leave_from
    assert apply_leave_from_days(p, [date(2026, 8, 7)]) is True
    assert p.leave_from == date(2026, 8, 4)
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
        [kirill, arturs], worklogs, week_monday=mon, week_friday=fri
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
    assert cov["updated_names"] == ["Kirill Rogovets"]
    assert p.status == PersonStatus.ON_LEAVE
    assert p.leave_until == date(2026, 8, 4)
