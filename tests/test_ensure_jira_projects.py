"""Ensure roster Jira keys resolve via Project/Account (no unmatched_project)."""

from __future__ import annotations

from designops.core.models import Account, Project
from designops.core.projects import ensure_projects_for_jira_keys
from designops.core.registry import ProjectRegistry


class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a, **k):
        # SQLAlchemy filter expressions — approximate for tests.
        return self

    def filter_by(self, **kw):
        out = []
        for r in self._rows:
            if all(getattr(r, k, None) == v for k, v in kw.items()):
                out.append(r)
        return _Q(out)

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, accounts, projects):
        self.accounts = accounts
        self.projects = projects
        self.added = []

    def query(self, model):
        if model is Account:
            return _Q(self.accounts)
        if model is Project:
            return _Q(self.projects)
        return _Q([])

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Project) and obj not in self.projects:
            self.projects.append(obj)
        if isinstance(obj, Account) and obj not in self.accounts:
            self.accounts.append(obj)

    def flush(self):
        pass


def test_ensure_links_fairwind_map_and_creates_internal():
    seedsman = Account(
        name="Seedsman",
        fairwind_account_id="fw-see",
        jira_project_keys=[],
        domains=[],
        digest_enabled=False,
        is_active=True,
        salesforce_account_ids=[],
        notion_space_ids=[],
        data_availability={},
        aliases=[],
    )
    session = _Session([seedsman], [])

    def get_project(key):
        return {
            "SEEUX": {"key": "SEEUX", "name": "Seedsman | UX/UI"},
            "SID": {"key": "SID", "name": "Scandiweb Internal Designs"},
            "GROW": {"key": "GROW", "name": "GROWTH TEAM | INTERNAL"},
        }.get(key)

    meta = ensure_projects_for_jira_keys(
        session,
        {"SEEUX", "SID", "GROW"},
        fairwind_jira_projects=[
            {
                "key": "SEEUX",
                "name": "Seedsman | UX/UI",
                "account": {"id": "fw-see", "name": "Seedsman"},
            }
        ],
        jira_get_project=get_project,
    )
    assert meta["ensured"] == 3
    assert "SEEUX" in (seedsman.jira_project_keys or [])
    assert any(p.jira_project_key == "SID" for p in session.projects)
    assert any(p.jira_project_key == "GROW" for p in session.projects)

    reg = ProjectRegistry.from_rows(session.projects, accounts=session.accounts)
    assert reg.resolve_jira_key("SEEUX") is not None
    assert reg.resolve_jira_key("SID").canonical_name == "Scandiweb Internal Designs"
    assert reg.resolve_jira_key("GROW") is not None


def test_ensure_name_matches_account_without_fairwind_map():
    reuzel = Account(
        name="Reuzel",
        fairwind_account_id="fw-rzl",
        jira_project_keys=[],
        domains=[],
        digest_enabled=False,
        is_active=True,
        salesforce_account_ids=[],
        notion_space_ids=[],
        data_availability={},
        aliases=[],
    )
    session = _Session([reuzel], [])
    meta = ensure_projects_for_jira_keys(
        session,
        {"RZL"},
        fairwind_jira_projects=[],
        jira_get_project=lambda k: {"key": "RZL", "name": "Reuzel | Growth marketing"},
    )
    assert meta["ensured"] == 1
    assert "RZL" in reuzel.jira_project_keys
    reg = ProjectRegistry.from_rows(session.projects, accounts=session.accounts)
    assert reg.resolve_jira_key("RZL").canonical_name == "Reuzel"
