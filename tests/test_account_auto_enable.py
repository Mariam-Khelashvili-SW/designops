"""Auto-enable Fairwind accounts from Jira project keys on the worked tickets."""

from __future__ import annotations

from types import SimpleNamespace

from designops.core.models import Account, Project
from designops.core.projects import (
    enable_accounts_for_jira_keys,
    jira_project_keys_from_docs,
)


def test_jira_project_keys_from_docs():
    docs = [
        SimpleNamespace(project_hint="ACERP1", raw={"key": "ACERP1-35"}, external_id="ACERP1-35"),
        SimpleNamespace(
            project_hint=None, raw={"project_key": "UOM", "key": "UOM-481"}, external_id="UOM-481"
        ),
        SimpleNamespace(project_hint=None, raw={}, external_id="SGDCP-12"),
    ]
    assert jira_project_keys_from_docs(docs) == {"ACERP1", "UOM", "SGDCP"}


class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_a, **_k):
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

    def flush(self):
        pass


def test_enable_accounts_for_jira_keys():
    acer = Account(
        name="Acer",
        fairwind_account_id="fw-acer",
        jira_project_keys=["ACERP1"],
        domains=[],
        digest_enabled=False,
        is_active=True,
        salesforce_account_ids=[],
        notion_space_ids=[],
        data_availability={},
        aliases=[],
    )
    other = Account(
        name="Other",
        fairwind_account_id="fw-other",
        jira_project_keys=["ZZZ"],
        domains=[],
        digest_enabled=False,
        is_active=True,
        salesforce_account_ids=[],
        notion_space_ids=[],
        data_availability={},
        aliases=[],
    )
    sgd = Account(
        name="Sports Group Denmark",
        fairwind_account_id="fw-sgd",
        jira_project_keys=[],
        domains=[],
        digest_enabled=False,
        is_active=True,
        salesforce_account_ids=[],
        notion_space_ids=[],
        data_availability={},
        aliases=[],
    )
    proj_sgd = Project(
        canonical_name="Sports Group Denmark",
        aliases=["SGD"],
        jira_project_key="SGDCP",
        fairwind_account_id="fw-sgd",
        active=True,
    )
    proj_acer = Project(
        canonical_name="Acer",
        aliases=["Acer"],
        jira_project_key="ACERP1",
        fairwind_account_id="fw-acer",
        active=True,
    )
    session = _Session([acer, other, sgd], [proj_sgd, proj_acer])

    newly = enable_accounts_for_jira_keys(session, {"ACERP1", "SGDCP"}, enabled_by="test")
    assert {a.name for a in newly} == {"Acer", "Sports Group Denmark"}
    assert acer.digest_enabled is True
    assert sgd.digest_enabled is True
    assert other.digest_enabled is False

    again = enable_accounts_for_jira_keys(session, {"ACERP1", "SGDCP"})
    assert again == []
