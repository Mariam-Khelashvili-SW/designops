"""Scheduler helpers for weekly-health Fairwind pre-warm."""

from datetime import datetime
from zoneinfo import ZoneInfo

from designops.api.scheduler import (
    cron_minus_minutes,
    describe_schedule,
    format_countdown,
    scheduled_report_date,
)


def test_cron_minus_minutes_same_day():
    assert cron_minus_minutes("0 12 * * tue", 30) == "30 11 * * tue"
    assert cron_minus_minutes("5 23 * * mon", 30) == "35 22 * * mon"


def test_cron_minus_minutes_wraps_midnight_and_dow():
    assert cron_minus_minutes("0 0 * * tue", 30) == "30 23 * * mon"
    assert cron_minus_minutes("15 0 * * 2", 30) == "45 23 * * 1"


def test_cron_minus_minutes_rejects_ranges():
    assert cron_minus_minutes("0 11 * * 1-5", 30) is None
    assert cron_minus_minutes("*/5 12 * * tue", 30) is None
    assert cron_minus_minutes("0 12 * *", 30) is None


def test_describe_schedule_weekdays():
    assert describe_schedule("0 12 * * mon-fri") == "12:00 Riga · Mon–Fri"
    assert describe_schedule("0 11 * * mon", "Europe/Riga") == "11:00 Riga · Mondays"
    assert "Tue–Sat" in describe_schedule("0 12 * * 1-5")


def test_normalize_weekday_cron():
    from designops.core.bootstrap import _normalize_weekday_cron

    assert _normalize_weekday_cron("0 12 * * 1-5") == "0 12 * * mon-fri"
    assert _normalize_weekday_cron("0 12 * * mon-fri") == "0 12 * * mon-fri"


def test_format_countdown():
    tz = ZoneInfo("Europe/Riga")
    now = datetime(2026, 8, 10, 9, 0, tzinfo=tz)
    nrt = datetime(2026, 8, 10, 12, 0, tzinfo=tz)
    assert format_countdown(nrt, now=now) == "in 3h"


def test_scheduled_report_date_skips_weekend():
    """Monday noon → Friday report; Tue → Mon; Fri → Thu."""
    from datetime import date

    tz = ZoneInfo("Europe/Riga")
    mon = datetime(2026, 8, 10, 12, 0, tzinfo=tz)  # Monday
    tue = datetime(2026, 8, 11, 12, 0, tzinfo=tz)
    fri = datetime(2026, 8, 14, 12, 0, tzinfo=tz)
    assert scheduled_report_date("daily-digest", mon) == date(2026, 8, 7)  # Fri
    assert scheduled_report_date("daily-digest", tue) == date(2026, 8, 10)  # Mon
    assert scheduled_report_date("daily-digest", fri) == date(2026, 8, 13)  # Thu
