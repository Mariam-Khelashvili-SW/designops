"""Scheduler helpers for weekly-health Fairwind pre-warm."""

from designops.api.scheduler import cron_minus_minutes


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
