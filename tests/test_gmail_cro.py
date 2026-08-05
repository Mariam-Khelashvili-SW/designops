"""CRO mailbox Gmail query constraints."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from designops.adapters.gmail import cro_mailbox_query


def test_is_late_morning_daily_cro():
    from designops.adapters.documents import Document
    from designops.pipelines.filter import is_late_morning_daily

    report = date(2026, 8, 4)
    next_day = date(2026, 8, 5)

    def cro(author: str, *, day, hour: int | None, eid: str) -> Document:
        return Document(
            source="gmail",
            external_id=eid,
            event_date=day,
            author_identity=author,
            title="Daily",
            body="…",
            sent_at=(
                None
                if hour is None
                else datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
            ),
            raw={"folder": "cro"},
        )

    assert is_late_morning_daily(cro("a@x.com", day=next_day, hour=8, eid="1"), report)
    assert is_late_morning_daily(cro("a@x.com", day=next_day, hour=1, eid="2"), report)
    assert not is_late_morning_daily(cro("a@x.com", day=next_day, hour=14, eid="3"), report)
    assert not is_late_morning_daily(cro("a@x.com", day=report, hour=9, eid="4"), report)


def test_cro_mailbox_query_requires_address():
    with pytest.raises(ValueError):
        cro_mailbox_query("")
    with pytest.raises(ValueError):
        cro_mailbox_query("not-an-email")


def test_cro_mailbox_query_always_scopes_to_cro():
    q = cro_mailbox_query("cro@scandiweb.com")
    assert "to:cro@scandiweb.com" in q
    assert "cc:cro@scandiweb.com" in q
    assert "deliveredto:cro@scandiweb.com" in q
    # Must not be a bare inbox query
    assert q.strip() != ""
    assert "in:inbox" not in q or "cro@scandiweb.com" in q


def test_cro_mailbox_query_date_bounds():
    q = cro_mailbox_query(
        "cro@scandiweb.com",
        after=date(2026, 7, 1),
        before=date(2026, 7, 31),
    )
    assert "after:2026-07-01" in q
    assert "before:2026-07-31" in q
    assert "to:cro@scandiweb.com" in q
