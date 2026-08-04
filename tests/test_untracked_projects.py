"""Registry prefers Fairwind-linked projects; untracked chip rules."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from designops.core.registry import ProjectRegistry
from designops.pipelines.daily_digest import _flag_untracked_projects


def test_registry_prefers_fairwind_linked_alias():
    orphan = SimpleNamespace(
        id=uuid4(),
        canonical_name="Enmedify",
        aliases=["Enmedify", "MediCrops"],
        jira_project_key=None,
        fairwind_account_id=None,
    )
    linked = SimpleNamespace(
        id=uuid4(),
        canonical_name="MediCrops",
        aliases=["MediCrops", "Enmedify", "ENUX"],
        jira_project_key="ENUX",
        fairwind_account_id="fw-med",
    )
    # Orphan listed first — still lose to linked.
    reg = ProjectRegistry.from_rows([orphan, linked])
    assert reg.resolve("Enmedify").canonical_name == "MediCrops"
    assert reg.resolve("Enmedify").fairwind_account_id == "fw-med"


def test_untracked_skips_internal_without_fairwind():
    linked = SimpleNamespace(
        id=uuid4(),
        canonical_name="MediCrops",
        aliases=["MediCrops", "Enmedify"],
        jira_project_key="ENUX",
        fairwind_account_id="fw-med",
    )
    internal = SimpleNamespace(
        id=uuid4(),
        canonical_name="SuperPD",
        aliases=["SuperPD", "SPDP"],
        jira_project_key="SPDP",
        fairwind_account_id=None,
    )
    reg = ProjectRegistry.from_rows([linked, internal])
    digest = {
        "status": [
            {"project": "Enmedify"},
            {"project": "SuperPD"},
            {"project": "UnknownCorp"},
        ]
    }
    _flag_untracked_projects(digest, reg, {"fw-med"}, {"MediCrops"})
    assert digest["status"][0]["untracked"] is False
    assert digest["status"][1]["untracked"] is False
    assert digest["status"][2]["untracked"] is True
