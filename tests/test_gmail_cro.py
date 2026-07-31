"""CRO mailbox Gmail query constraints."""

from __future__ import annotations

from datetime import date

import pytest

from designops.adapters.gmail import cro_mailbox_query


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
