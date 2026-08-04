"""Email-first identity resolution (§2 fix #1/#3/#8, §11.3).

Match on identity — email or jira accountId — NEVER on substring or display name.
This is what keeps Elene Minashvili (PM) and the other collisions in §11.3 out of a
design digest. Display names are carried for rendering only.

Pure: `RosterIndex` is built from any objects exposing id/full_name/emails/
jira_account_id/status (ORM `Person` rows or plain fixtures), so the filter and its
tests need no database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from designops.core.enums import PersonStatus


def effective_status(
    status: str,
    leave_until: date | None,
    report_date: date | None,
    leave_from: date | None = None,
) -> str:
    """A person's status FOR a given report day.

    `on_leave` reverts to `active` when the report day is outside the leave window:
    after `leave_until`, or (when `leave_from` is set) before leave starts. Weekly
    availability keeps using week-level PARTIAL math and typically omits `leave_from`
    here so Mon of a partial-leave week still resolves as on_leave.
    """
    if status != PersonStatus.ON_LEAVE or report_date is None:
        return status
    if leave_until and report_date > leave_until:
        return PersonStatus.ACTIVE
    if leave_from and report_date < leave_from:
        return PersonStatus.ACTIVE
    return status


@dataclass(frozen=True, slots=True)
class RosterMember:
    id: uuid.UUID
    full_name: str
    emails: frozenset[str]  # normalized lowercase
    jira_account_id: str | None
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == PersonStatus.ACTIVE


class RosterIndex:
    def __init__(self, members: list[RosterMember]):
        self._members = members
        self._by_email: dict[str, RosterMember] = {}
        self._by_jira: dict[str, RosterMember] = {}
        for m in members:
            for e in m.emails:
                self._by_email[e] = m
            if m.jira_account_id:
                self._by_jira[m.jira_account_id] = m

    @classmethod
    def from_rows(cls, rows, report_date: date | None = None) -> RosterIndex:
        """Build the roster for a report day — leave outside [leave_from, leave_until]
        is treated as `active` again (leave auto-expires / not-yet-started)."""
        return cls(
            [
                RosterMember(
                    id=r.id,
                    full_name=r.full_name,
                    emails=frozenset(e.lower() for e in (r.emails or [])),
                    jira_account_id=r.jira_account_id,
                    status=effective_status(
                        r.status,
                        getattr(r, "leave_until", None),
                        report_date,
                        leave_from=getattr(r, "leave_from", None),
                    ),
                )
                for r in rows
            ]
        )

    def resolve(self, author_identity: str | None) -> RosterMember | None:
        """Exact match on email or jira accountId. None = out of scope."""
        if not author_identity:
            return None
        key = author_identity.strip()
        return self._by_email.get(key.lower()) or self._by_jira.get(key)

    @property
    def active_members(self) -> list[RosterMember]:
        return [m for m in self._members if m.is_active]

    @property
    def active_count(self) -> int:
        return len(self.active_members)
