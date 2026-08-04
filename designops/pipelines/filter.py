"""Deterministic scope filter (§6.2, §11.2) — the load-bearing 'scope is code' stage.

PURE and fast: `(documents, roster, registry, report_date) -> FilterResult`. No LLM,
no DB, no network. The model never sees anything this stage drops.

Decision order per document (first match wins for the exclusion reason recorded):
  1. duplicate           -> duplicate_of:{external_id}     (dedup before synthesis, §11.2)
  2. not in roster       -> not_in_roster                  (§2 fix #3)
  3. roster but not active -> roster_out                   (§2 fix #7)
  4. dated after report  -> after_report_day               (§2 fix #5)
  5. Jira time-log bucket -> time_log_bucket                (§2 fix #4 budget math, §7.2)
Survivors are INCLUDED; project resolution never drops a doc — an unmatched project
string keeps the doc, buckets it Unassigned, and raises an unmatched_project flag.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from designops.adapters.documents import Document
from designops.core.enums import TIME_LOG_BUCKET_TYPES, ExclusionReason
from designops.core.identity import RosterIndex, RosterMember
from designops.core.registry import ProjectEntry, ProjectRegistry

# Designers sometimes write a day's report the next morning. For INTERNAL design dailies
# we therefore accept the report day OR the next morning; client-facing (external) email
# and Jira stay strictly on the report day. "Morning" = sent before this hour.
_LATE_REPORT_CUTOFF_HOUR = 12


def _is_internal_daily(doc: Document) -> bool:
    """Design-team daily report channel.

    Fairwind ``folder=internal`` threads, or mail a roster designer sent to cro@
    (the Design team inbox — where many dailies actually land).
    """
    folder = (doc.raw or {}).get("folder")
    return folder == "internal" or (
        doc.source == "gmail" and folder == "cro"
    )


def _is_beyond_daily(doc: Document, report_date: date) -> bool:
    """Report-day, non-roster signal the model may mine for blockers/escalations no daily
    carried: meeting transcript, outbound client-facing Fairwind email, or cro@ mailbox mail
    from someone *not* on the design roster (client / other). Report DAY only.
    """
    if doc.event_date != report_date:
        return False
    if doc.source == "transcript":
        return True
    # CRO shared inbox — only non-roster authors (roster cro mail = their daily).
    if doc.source == "gmail" and doc.raw.get("folder") == "cro":
        return True
    return (
        doc.raw.get("folder") == "external"
        and doc.author_identity.endswith("@scandiweb.com")
    )


def _temporal_verdict(doc: Document, report_date: date, next_day: date) -> str | None:
    """None = in scope; otherwise the exclusion reason. Channel-aware (§ user 21 Jul):
    Jira + external email → report day only; internal dailies → report day or next AM."""
    ed = doc.event_date
    if ed == report_date:
        return None
    if _is_internal_daily(doc) and ed == next_day:
        # a late daily written the next morning still belongs to the report day
        if doc.sent_at is None or doc.sent_at.hour < _LATE_REPORT_CUTOFF_HOUR:
            return None
        return str(ExclusionReason.AFTER_REPORT_DAY)
    return str(
        ExclusionReason.AFTER_REPORT_DAY if ed > report_date
        else ExclusionReason.BEFORE_REPORT_DAY
    )


@dataclass(slots=True)
class AuditRecord:
    """One row destined for `run_document` — the full 'what was read AND why dropped'."""

    source: str
    external_id: str
    event_date: date | None
    person_id: uuid.UUID | None
    project_id: uuid.UUID | None
    included: bool
    exclusion_reason: str | None
    title: str | None


@dataclass(slots=True)
class IncludedDoc:
    document: Document
    person: RosterMember
    project: ProjectEntry | None  # None = Unassigned bucket


@dataclass(slots=True)
class BeyondDailyDoc:
    """A report-day non-daily signal (client email / transcript) the model may mine for
    blockers/escalations no daily carried. Not attributed to a person."""

    document: Document
    project: ProjectEntry | None
    label: str = "Unassigned"  # project name, else the source account name


@dataclass(slots=True)
class FilterResult:
    included: list[IncludedDoc] = field(default_factory=list)
    beyond_daily: list[BeyondDailyDoc] = field(default_factory=list)
    audit: list[AuditRecord] = field(default_factory=list)
    unmatched_projects: dict[str, str] = field(default_factory=dict)  # raw string -> a doc id
    coverage_ratio: float = 0.0
    reported_person_ids: set[uuid.UUID] = field(default_factory=set)
    silent_person_ids: set[uuid.UUID] = field(default_factory=set)
    duplicate_count: int = 0

    def counts(self) -> dict:
        return {
            "read": len(self.audit),
            "included": len(self.included),
            "beyond_daily": len(self.beyond_daily),
            "excluded": sum(1 for a in self.audit if not a.included),
            "duplicates": self.duplicate_count,
            "unmatched_projects": len(self.unmatched_projects),
            "reported": len(self.reported_person_ids),
            "silent": len(self.silent_person_ids),
            "coverage_ratio": round(self.coverage_ratio, 3),
        }


def filter_corpus(
    documents: list[Document],
    roster: RosterIndex,
    registry: ProjectRegistry,
    report_date: date,
    account_names: dict[str, str] | None = None,
) -> FilterResult:
    result = FilterResult()
    account_names = account_names or {}

    # --- 1. dedup before anything else (§11.2): model must see each daily once ---
    seen: dict[str, str] = {}  # dedup_key -> external_id of the kept copy
    deduped: list[Document] = []
    for doc in documents:
        key = doc.dedup_key()
        if key in seen:
            result.duplicate_count += 1
            result.audit.append(
                AuditRecord(
                    source=doc.source,
                    external_id=doc.external_id,
                    event_date=doc.event_date,
                    person_id=None,
                    project_id=None,
                    included=False,
                    exclusion_reason=f"duplicate_of:{seen[key]}",
                    title=doc.title,
                )
            )
            continue
        seen[key] = doc.external_id
        deduped.append(doc)

    # --- 2..5 per-document scope checks ---
    next_day = report_date + timedelta(days=1)
    for doc in deduped:
        member = roster.resolve(doc.author_identity)

        if member is None:
            # Not a roster daily. Keep it ONLY if it is report-day beyond-daily signal
            # (client email our team sent, or a transcript); otherwise drop it.
            if _is_beyond_daily(doc, report_date):
                project = registry.resolve_account(doc.account_id) or registry.resolve(
                    doc.project_hint
                )
                # label = canonical project if the account maps to one; otherwise the
                # source account's own name (never a bare "Unassigned" for a known account)
                label = (
                    project.canonical_name
                    if project
                    else account_names.get(doc.account_id or "", "Unassigned")
                )
                result.beyond_daily.append(
                    BeyondDailyDoc(document=doc, project=project, label=label)
                )
                result.audit.append(
                    AuditRecord(
                        source=doc.source, external_id=doc.external_id,
                        event_date=doc.event_date, person_id=None,
                        project_id=project.id if project else None,
                        included=False, exclusion_reason="beyond_daily",
                        title=doc.title,
                    )
                )
            else:
                _drop(result, doc, ExclusionReason.NOT_IN_ROSTER)
            continue
        if not member.is_active:
            _drop(result, doc, ExclusionReason.ROSTER_OUT, person_id=member.id)
            continue
        temporal = _temporal_verdict(doc, report_date, next_day)
        if temporal is not None:
            _drop(result, doc, temporal, person_id=member.id)
            continue
        if doc.source == "jira" and (doc.jira_issue_type in TIME_LOG_BUCKET_TYPES):
            _drop(result, doc, ExclusionReason.TIME_LOG_BUCKET, person_id=member.id)
            continue

        # resolve project — never drops the doc (§6.2)
        project = registry.resolve_jira_key(doc.project_hint) if doc.source == "jira" else None
        if project is None:
            project = registry.resolve(doc.project_hint)
        if project is None and doc.project_hint:
            result.unmatched_projects.setdefault(doc.project_hint, doc.external_id)

        doc.person_id = member.id
        doc.project_id = project.id if project else None
        result.included.append(IncludedDoc(document=doc, person=member, project=project))
        # "reported" = filed a design daily (Fairwind internal thread OR cro@ from roster).
        # Jira tickets stay cross-checks only — not a substitute for the written daily.
        if _is_internal_daily(doc):
            result.reported_person_ids.add(member.id)
        result.audit.append(
            AuditRecord(
                source=doc.source,
                external_id=doc.external_id,
                event_date=doc.event_date,
                person_id=member.id,
                project_id=doc.project_id,
                included=True,
                exclusion_reason=None,
                title=doc.title,
            )
        )

    # --- coverage: reported active members / active roster (§1, §11.1) ---
    active = {m.id for m in roster.active_members}
    reported_active = result.reported_person_ids & active
    result.reported_person_ids = reported_active
    result.silent_person_ids = active - reported_active
    result.coverage_ratio = (len(reported_active) / len(active)) if active else 0.0
    return result


def _drop(
    result: FilterResult,
    doc: Document,
    reason: str,
    *,
    person_id: uuid.UUID | None = None,
) -> None:
    result.audit.append(
        AuditRecord(
            source=doc.source,
            external_id=doc.external_id,
            event_date=doc.event_date,
            person_id=person_id,
            project_id=None,
            included=False,
            exclusion_reason=str(reason),
            title=doc.title,
        )
    )
