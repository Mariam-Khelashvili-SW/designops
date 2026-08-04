"""Daily Fairwind scope = Jira ticket projects (+ mention second pass), not all enabled."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from designops.adapters.documents import Document
from designops.pipelines import daily_digest as dd


def test_resolve_daily_fairwind_scope_uses_jira_not_all_enabled(monkeypatch):
    report_date = date(2026, 8, 3)
    roster = [
        SimpleNamespace(
            id="p1",
            status="active",
            leave_until=None,
            leave_from=None,
            emails=["a@x.com"],
            jira_account_id="jira-1",
        )
    ]
    jira_doc = Document(
        source="jira",
        external_id="ACERP1-1",
        event_date=report_date,
        title="Task",
        body="",
        author_identity="jira-1",
        project_hint="ACERP1",
        raw={"key": "ACERP1-1", "project_key": "ACERP1"},
    )

    class FakeSettings:
        jira_configured = True
        fairwind_configured = False
        setup_owner_email = "ops@example.com"

    monkeypatch.setattr(dd, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(
        "designops.adapters.jira.resolve_roster_account_ids",
        lambda people, persist=True, settings=None: {"p1": "jira-1"},
    )

    class FakeJira:
        def __init__(self, _settings=None):
            pass

        def search_open_assigned(self, *_a, **_k):
            return [jira_doc]

        def get_project(self, key):
            return {"key": key, "name": key}

    monkeypatch.setattr("designops.adapters.jira.JiraClient", FakeJira)

    session = MagicMock()
    session.flush = MagicMock()

    monkeypatch.setattr(dd, "fairwind_account_ids_for_jira_keys", lambda _s, keys: {"fw-acer"} if keys else set())
    monkeypatch.setattr(dd, "enable_accounts_for_jira_keys", lambda *_a, **_k: [])
    monkeypatch.setattr(
        dd,
        "ensure_projects_for_jira_keys",
        lambda *_a, **_k: {"ensured": 0, "linked": [], "created": [], "already": []},
    )
    monkeypatch.setattr(
        dd, "_digest_enabled_account_ids", lambda _s: ["fw-all-1", "fw-all-2", "fw-acer"]
    )

    ids, docs, meta = dd.resolve_daily_fairwind_scope(session, roster, report_date)
    assert ids == ["fw-acer"]
    assert docs == [jira_doc]
    assert meta["fairwind_scope"] == "jira"
    assert meta["jira_project_keys"] == ["ACERP1"]


def test_resolve_daily_fairwind_scope_falls_back_without_jira(monkeypatch):
    class FakeSettings:
        jira_configured = False
        setup_owner_email = None

    monkeypatch.setattr(dd, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(
        dd, "_digest_enabled_account_ids", lambda _s: ["fw-a", "fw-b"]
    )
    ids, docs, meta = dd.resolve_daily_fairwind_scope(MagicMock(), [], date(2026, 8, 3))
    assert ids == ["fw-a", "fw-b"]
    assert docs == []
    assert meta["fairwind_scope"] == "digest_enabled_fallback"
