"""Daily ingest: Jira context only, then Fairwind for mentioned accounts."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from designops.adapters.documents import Document
from designops.pipelines import daily_digest as dd


def test_resolve_daily_jira_context_no_fairwind_ids(monkeypatch):
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
    monkeypatch.setattr(
        dd,
        "ensure_projects_for_jira_keys",
        lambda *_a, **_k: {"ensured": 0, "linked": [], "created": [], "already": []},
    )
    monkeypatch.setattr(dd, "enable_accounts_for_jira_keys", lambda *_a, **_k: [])

    docs, meta = dd.resolve_daily_jira_context(session, roster, report_date)
    assert docs == [jira_doc]
    assert meta["jira_project_keys"] == ["ACERP1"]
    assert meta["jira_docs"] == 1
    # Compat wrapper returns empty Fairwind ids (mentions drive exports).
    ids, docs2, meta2 = dd.resolve_daily_fairwind_scope(session, roster, report_date)
    assert ids == []
    assert docs2 == [jira_doc]
    assert meta2["fairwind_scope"] == "mentions"


def test_resolve_daily_jira_context_without_jira(monkeypatch):
    class FakeSettings:
        jira_configured = False
        fairwind_configured = False
        setup_owner_email = None

    monkeypatch.setattr(dd, "get_settings", lambda: FakeSettings())
    docs, meta = dd.resolve_daily_jira_context(MagicMock(), [], date(2026, 8, 3))
    assert docs == []
    assert meta["jira_docs"] == 0
    assert "not configured" in (meta.get("jira_note") or "").lower()


def test_ingest_skips_fairwind_when_account_ids_empty(monkeypatch):
    class FakeSettings:
        fairwind_configured = True
        cro_mailbox_email = "cro@scandiweb.com"

    monkeypatch.setattr(dd, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(dd, "_ingest_cro", lambda *_a, **_k: ([], {"cro_messages": 0}))
    docs, cov = dd._ingest(MagicMock(), date(2026, 8, 3), reuse=False, account_ids=[])
    assert docs == []
    assert cov["source"] == "fairwind-skipped"
    assert cov["accounts_requested"] == 0


def test_mention_pass_uses_external_and_transcripts(monkeypatch):
    """Pass B prepare_corpus must request emails_external + transcripts only."""
    seen = {}

    class FakeClient:
        def prepare_corpus(
            self, ids, report_date, *, window_end=None, data_types=None, concurrency=None, **_k
        ):
            seen["ids"] = list(ids)
            seen["data_types"] = list(data_types or [])
            seen["concurrency"] = concurrency
            return [], {
                "exports_succeeded": 0,
                "exports_failed": 0,
                "failed_accounts": [],
                "accounts_requested": len(ids),
                "concurrency": concurrency or 1,
            }

    monkeypatch.setattr(dd, "FairwindClient", lambda _s=None: FakeClient())
    monkeypatch.setattr(
        dd,
        "save_corpus",
        lambda *_a, **_k: None,
    )

    client = dd.FairwindClient()
    docs, cov = client.prepare_corpus(
        ["fw-north"],
        date(2026, 8, 3),
        window_end=date(2026, 8, 4),
        data_types=list(dd.MENTION_DATA_TYPES),
        concurrency=8,
    )
    assert seen["ids"] == ["fw-north"]
    assert seen["data_types"] == ["emails_internal", "emails_external", "transcripts"]
    assert seen["concurrency"] == 8
    assert dd.MENTION_DATA_TYPES == ["emails_internal", "emails_external", "transcripts"]


def test_prepare_corpus_caps_concurrency_to_account_count(monkeypatch):
    from designops.adapters import fairwind as fw

    calls = {"workers": None}

    class FakeSettings:
        fairwind_configured = True
        fw_base_url = "https://example.test"
        fw_export_concurrency = 8
        fw_export_poll_interval_s = 2.0
        fw_data_types = ["emails_external"]
        corpus_store_dir = "/tmp/corpus-test"

    monkeypatch.setattr(fw.httpx, "Client", lambda **_k: MagicMock())
    client = fw.FairwindClient(FakeSettings())  # type: ignore[arg-type]

    class CapturingPool:
        def __init__(self, max_workers=None):
            calls["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, ids):
            return []

    monkeypatch.setattr(fw, "ThreadPoolExecutor", CapturingPool)
    docs, cov = client.prepare_corpus(
        ["a", "b", "c"],
        date(2026, 8, 3),
        concurrency=8,
        data_types=["emails_external", "transcripts"],
    )
    assert calls["workers"] == 3  # capped to account count
    assert cov["concurrency"] == 3
    assert cov["poll_interval_s"] == 2.0
