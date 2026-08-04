"""Auto-link Account.jira_project_keys → Project so unmatched_project flags don't fire."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from designops.core.projects import (
    _apply_account_jira_keys,
    absorb_jira_keys_from_docs,
    sync_jira_keys_to_projects,
)
from designops.core.registry import ProjectRegistry


def test_apply_copies_all_account_keys_as_aliases():
    project = SimpleNamespace(jira_project_key=None, aliases=["Amiel"])
    account = SimpleNamespace(jira_project_keys=["AAD", "AMIEL", "AMIELH"])
    assert _apply_account_jira_keys(project, account) is True
    assert project.jira_project_key == "AAD"
    # AMIEL is skipped as an alias when "Amiel" already covers it case-insensitively
    assert "AAD" in project.aliases and "AMIELH" in project.aliases
    assert "Amiel" in project.aliases
    # Idempotent
    assert _apply_account_jira_keys(project, account) is False

    reg = ProjectRegistry.from_rows(
        [
            SimpleNamespace(
                id=uuid4(),
                canonical_name="Amiel",
                aliases=project.aliases,
                jira_project_key=project.jira_project_key,
                fairwind_account_id="fw",
            )
        ]
    )
    assert reg.resolve_jira_key("AAD").canonical_name == "Amiel"
    assert reg.resolve_jira_key("AMIEL").canonical_name == "Amiel"
    assert reg.resolve_jira_key("AMIELH").canonical_name == "Amiel"


def test_registry_indexes_account_jira_keys():
    pid = uuid4()
    project = SimpleNamespace(
        id=pid,
        canonical_name="Discover Cars",
        aliases=["Discover Cars"],
        jira_project_key=None,
        fairwind_account_id="fw-dcp",
    )
    account = SimpleNamespace(
        fairwind_account_id="fw-dcp",
        jira_project_keys=["DCP1"],
    )
    reg = ProjectRegistry.from_rows([project], accounts=[account])
    assert reg.resolve_jira_key("DCP1") is not None
    assert reg.resolve_jira_key("DCP1").canonical_name == "Discover Cars"
    assert reg.resolve("DCP1") is not None


def test_absorb_learns_new_board_key(monkeypatch):
    """New Jira board on an existing Fairwind account → appended to account keys."""
    from designops.core import projects as proj_mod

    acct = SimpleNamespace(
        name="JYSK",
        fairwind_account_id="fw-jysk",
        jira_project_keys=["JYSKB"],
    )
    project = SimpleNamespace(
        id=uuid4(),
        canonical_name="JYSK",
        aliases=["JYSK"],
        jira_project_key=None,
        fairwind_account_id="fw-jysk",
    )

    class Q:
        def __init__(self, row):
            self._row = row

        def filter_by(self, **kw):
            return self

        def first(self):
            return self._row

        def one_or_none(self):
            return self._row

        def all(self):
            return [self._row]

    class Session:
        def query(self, model):
            name = getattr(model, "__name__", "")
            if name == "Account":
                return Q(acct)
            return Q(project)

        def add(self, _obj):
            return None

        def flush(self):
            return None

    docs = [
        SimpleNamespace(
            source="jira",
            account_id="fw-jysk",
            project_hint="JYDP",
            external_id="JYDP-12",
            raw={"key": "JYDP-12", "project_key": "JYDP"},
        )
    ]
    out = absorb_jira_keys_from_docs(Session(), docs)
    assert out["accounts_learned"] == 1
    assert project.jira_project_key == "JYSKB"
    assert "JYDP" in project.aliases


def test_sync_sets_missing_project_key_from_account():
    """The DCP1/AAD/JYDP gap: account had keys, project.jira_project_key was null."""
    acct = SimpleNamespace(
        name="Discover Cars",
        fairwind_account_id="fw-dcp",
        jira_project_keys=["DCP1"],
    )
    project = SimpleNamespace(
        id=uuid4(),
        canonical_name="Discover Cars",
        aliases=["Discover Cars"],
        jira_project_key=None,
        fairwind_account_id="fw-dcp",
    )

    class Q:
        def __init__(self, rows):
            self._rows = rows if isinstance(rows, list) else [rows]

        def filter_by(self, **kw):
            if "fairwind_account_id" in kw:
                matched = [
                    r
                    for r in self._rows
                    if getattr(r, "fairwind_account_id", None) == kw["fairwind_account_id"]
                ]
                return Q(matched)
            if "canonical_name" in kw:
                matched = [
                    r
                    for r in self._rows
                    if getattr(r, "canonical_name", None) == kw["canonical_name"]
                    or getattr(r, "name", None) == kw["canonical_name"]
                ]
                return Q(matched)
            return self

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

    out = sync_jira_keys_to_projects(Session(), docs=[])
    assert project.jira_project_key == "DCP1"
    assert "DCP1" in project.aliases
    assert out["projects_synced"] == 1
