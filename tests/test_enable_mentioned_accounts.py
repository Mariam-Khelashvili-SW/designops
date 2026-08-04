"""Auto-enable Fairwind accounts when designers mention the project."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from designops.core.projects import (
    collect_mentioned_fairwind_ids,
    enable_accounts_for_mentioned_projects,
)
from designops.core.registry import ProjectRegistry


def test_collect_mentioned_from_body_and_hint():
    pid = uuid4()
    project = SimpleNamespace(
        id=pid,
        canonical_name="Northerner",
        aliases=["Northerner", "Nicokick"],
        jira_project_key=None,
        fairwind_account_id="fw-north",
    )
    tempo = SimpleNamespace(
        id=uuid4(),
        canonical_name="Tempo",
        aliases=["Tempo"],
        jira_project_key=None,
        fairwind_account_id="fw-tempo",
    )
    reg = ProjectRegistry.from_rows([project, tempo])
    docs = [
        SimpleNamespace(
            source="email",
            project_hint=None,
            title="Daily",
            body="Continued Nicokick subscription LP with Vlad. Logged Tempo later.",
            raw={"folder": "internal"},
        ),
        SimpleNamespace(
            source="jira",
            project_hint="NCO",
            title="Task",
            body="Weekend ship",
            raw={},
        ),
    ]
    # Internal daily → Nicokick (≥6). Tempo is only 5 chars → not free-text enabled.
    # Jira NCO is short; no exact registry key → ignored.
    ids = collect_mentioned_fairwind_ids(docs, reg)
    assert ids == {"fw-north"}


def test_enable_accounts_for_mentioned_projects():
    acct = SimpleNamespace(
        name="Northerner",
        fairwind_account_id="fw-north",
        digest_enabled=False,
        enabled_by=None,
        enabled_at=None,
        notes=None,
        jira_project_keys=["NCO"],
    )
    project = SimpleNamespace(
        id=uuid4(),
        canonical_name="Northerner",
        aliases=["Northerner"],
        jira_project_key=None,
        fairwind_account_id="fw-north",
    )

    class Q:
        def __init__(self, rows):
            self._rows = rows if isinstance(rows, list) else [rows]

        def filter_by(self, **kw):
            matched = self._rows
            if "fairwind_account_id" in kw:
                matched = [
                    r
                    for r in self._rows
                    if getattr(r, "fairwind_account_id", None) == kw["fairwind_account_id"]
                ]
            return Q(matched)

        def first(self):
            return self._rows[0] if self._rows else None

        def one_or_none(self):
            return self._rows[0] if self._rows else None

        def all(self):
            return list(self._rows)

    class Session:
        def query(self, model):
            name = getattr(model, "__name__", "")
            if name == "Account":
                return Q([acct])
            return Q([project])

        def add(self, _obj):
            return None

        def flush(self):
            return None

    newly = enable_accounts_for_mentioned_projects(
        Session(),
        fairwind_account_ids={"fw-north"},
        enabled_by="test",
    )
    assert len(newly) == 1
    assert acct.digest_enabled is True
    assert "mentioned" in (acct.notes or "").lower()
    # Idempotent
    assert (
        enable_accounts_for_mentioned_projects(
            Session(), fairwind_account_ids={"fw-north"}
        )
        == []
    )
