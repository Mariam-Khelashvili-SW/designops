"""Shared fixtures. Everything here is pure — no DB, no network — so the scope
tests run with `pytest -m "not db and not llm and not fairwind"`.

Roster and registry are built from the REAL seed YAMLs, so the tests validate the
actual shipped config (e.g. the Northerner aliases the §7.2 test depends on).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from designops.adapters.documents import Document
from designops.core.identity import RosterIndex, RosterMember
from designops.core.registry import ProjectEntry, ProjectRegistry

SEEDS = Path(__file__).resolve().parent.parent / "designops" / "seeds"
_NS = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _det_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(_NS, key)


@pytest.fixture(scope="session")
def roster_seed() -> dict:
    with open(SEEDS / "roster.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def project_seed() -> dict:
    with open(SEEDS / "projects.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def roster(roster_seed) -> RosterIndex:
    members = []
    for row in roster_seed["people"]:
        emails = frozenset(e.lower() for e in (row.get("emails") or []))
        key = next(iter(emails)) if emails else row["full_name"]
        members.append(
            RosterMember(
                id=_det_uuid(key),
                full_name=row["full_name"],
                emails=emails,
                jira_account_id=row.get("jira_account_id"),
                status=row.get("status", "active"),
            )
        )
    return RosterIndex(members)


@pytest.fixture
def registry(project_seed) -> ProjectRegistry:
    entries = []
    for row in project_seed["projects"]:
        entries.append(
            (
                ProjectEntry(
                    id=_det_uuid(row["canonical_name"]),
                    canonical_name=row["canonical_name"],
                    jira_project_key=row.get("jira_project_key"),
                    fairwind_account_id=row.get("fairwind_account_id"),
                ),
                list(row.get("aliases") or []),
            )
        )
    return ProjectRegistry(entries)


@pytest.fixture
def guards(roster_seed) -> dict[str, str]:
    """display_name -> email for the §11.3 collision decoys (PMs/devs, NOT roster)."""
    return {g["full_name"]: g["email"] for g in roster_seed.get("identity_guards", [])}


# ---- Document builders -------------------------------------------------------

REPORT_DATE = date(2026, 7, 17)  # Friday 17 Jul 2026 (§7.1)


def make_email(
    *,
    author: str,
    body: str = "daily report body",
    event_date: date = REPORT_DATE,
    external_id: str | None = None,
    message_id: str | None = None,
    project_hint: str | None = None,
    title: str = "Daily report",
    sent_at: datetime | None = None,
    account_id: str | None = None,
    folder: str = "internal",  # internal = design daily; external = client-facing
) -> Document:
    return Document(
        source="fairwind",
        external_id=external_id or f"email-{uuid.uuid4().hex[:8]}",
        event_date=event_date,
        author_identity=author,
        title=title,
        body=body,
        message_id=message_id,
        project_hint=project_hint,
        account_id=account_id,
        sent_at=sent_at or datetime(event_date.year, event_date.month, event_date.day, 9, 0),
        raw={"folder": folder},
    )


def make_jira(
    *,
    assignee_account_id: str,
    issue_type: str = "Task",
    event_date: date = REPORT_DATE,
    project_hint: str | None = None,
    external_id: str | None = None,
    title: str = "UX ticket",
) -> Document:
    return Document(
        source="jira",
        external_id=external_id or f"jira-{uuid.uuid4().hex[:8]}",
        event_date=event_date,
        author_identity=assignee_account_id,
        title=title,
        body="issue body",
        jira_issue_type=issue_type,
        project_hint=project_hint,
    )
