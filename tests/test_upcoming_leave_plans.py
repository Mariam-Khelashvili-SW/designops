"""Upcoming leave on the next working day → Today's plans."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from designops.core.enums import PersonStatus
from designops.pipelines.daily_digest import (
    _annotate_upcoming_leave,
    next_working_day,
)


def test_next_working_day_skips_weekend():
    assert next_working_day(date(2026, 8, 7)) == date(2026, 8, 10)  # Fri → Mon
    assert next_working_day(date(2026, 8, 4)) == date(2026, 8, 5)  # Tue → Wed


def test_annotate_upcoming_leave_in_plans():
    """Leave starts Wednesday; report Tuesday → show in plans, not if already out today."""
    dorota = SimpleNamespace(
        id=uuid4(),
        full_name="Dorota Umiastowska",
        status=PersonStatus.ON_LEAVE,
        leave_from=date(2026, 8, 5),
        leave_until=date(2026, 8, 7),
    )
    kirill = SimpleNamespace(
        id=uuid4(),
        full_name="Kirill Rogovets",
        status=PersonStatus.ON_LEAVE,
        leave_from=date(2026, 8, 4),
        leave_until=date(2026, 8, 7),
    )
    digest = {
        "todays_plans": [
            {"person": "Dorota Umiastowska", "plan": "Finish Amiel header."},
        ]
    }
    # Report Tue 4 Aug — Dorota leave starts Wed 5; Kirill already on leave Tue
    _annotate_upcoming_leave(digest, [dorota, kirill], date(2026, 8, 4))
    by = {p["person"]: p for p in digest["todays_plans"]}
    assert "Dorota Umiastowska" in by
    assert "On leave" in by["Dorota Umiastowska"]["plan"]
    assert "Finish Amiel header" in by["Dorota Umiastowska"]["plan"]
    assert "Kirill Rogovets" not in by  # already out on report day


def test_annotate_leave_without_daily():
    person = SimpleNamespace(
        id=uuid4(),
        full_name="Predrag Gavrilovikj",
        status=PersonStatus.ON_LEAVE,
        leave_from=date(2026, 8, 5),
        leave_until=date(2026, 8, 5),
    )
    digest = {"todays_plans": []}
    _annotate_upcoming_leave(digest, [person], date(2026, 8, 4))
    assert len(digest["todays_plans"]) == 1
    row = digest["todays_plans"][0]
    assert row["person"] == "Predrag Gavrilovikj"
    assert row.get("leave_upcoming") is True
    assert "On leave" in row["plan"]
