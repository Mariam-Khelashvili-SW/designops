"""§7.2 scope-filter tests — pure, no LLM, must be fast.

Run: pytest -m "not db and not llm and not fairwind"  (these carry no marker).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from designops.core.enums import ExclusionReason
from designops.core.identity import RosterIndex, RosterMember
from designops.pipelines.filter import filter_corpus

from .conftest import REPORT_DATE, make_email, make_jira

MONDAY_AFTER = date(2026, 7, 20)  # Monday after the Friday report_date
NEXT_DAY = REPORT_DATE + timedelta(days=1)  # Sat 18 Jul — the "next morning" window


def _reason(result, external_id: str) -> str | None:
    rec = next(a for a in result.audit if a.external_id == external_id)
    return rec.exclusion_reason


def _included_ids(result) -> set[str]:
    return {i.document.external_id for i in result.included}


# --- §7.2.1 Elene collision: Minashvili never in, Chekurishvili always in --------
def test_minashvili_never_enters_chekurishvili_always(roster, registry, guards):
    chekurishvili = "elene.chekurishvili@scandiweb.com"
    minashvili = guards["Elene Minashvili"]  # PM decoy, same display name
    assert minashvili != chekurishvili

    docs = [
        make_email(author=chekurishvili, external_id="good", title="Elene C daily"),
        make_email(author=minashvili, external_id="pm", title="Elene M (PM) update"),
    ]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)

    assert "good" in _included_ids(result)
    assert "pm" not in _included_ids(result)
    assert _reason(result, "pm") == str(ExclusionReason.NOT_IN_ROSTER)


# --- §7.2.2 dev/QA sender excluded not_in_roster --------------------------------
def test_dev_qa_sender_excluded(roster, registry):
    docs = [make_email(author="some.dev@scandiweb.com", external_id="dev")]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    assert not _included_ids(result)
    assert _reason(result, "dev") == str(ExclusionReason.NOT_IN_ROSTER)


# --- §7.2.3 Monday-dated Jira transition excluded from a Friday report_date ------
def test_after_report_day_jira(registry):
    elene = RosterMember(
        id=uuid.uuid4(),
        full_name="Elene Chekurishvili",
        emails=frozenset({"elene.chekurishvili@scandiweb.com"}),
        jira_account_id="acct-elene",
        status="active",
    )
    roster = RosterIndex([elene])
    docs = [
        make_jira(assignee_account_id="acct-elene", event_date=MONDAY_AFTER,
                  external_id="mon", project_hint="Northerner"),
        make_jira(assignee_account_id="acct-elene", event_date=REPORT_DATE,
                  external_id="fri", project_hint="Northerner"),
    ]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    assert _included_ids(result) == {"fri"}
    assert _reason(result, "mon") == str(ExclusionReason.AFTER_REPORT_DAY)


# --- next-morning late report: internal daily kept next AM; client/Jira stay strict ---
def test_next_morning_internal_daily_kept_client_and_jira_strict(roster, registry):
    author = "elene.chekurishvili@scandiweb.com"
    elene = RosterMember(id=uuid.uuid4(), full_name="Elene Chekurishvili",
                         emails=frozenset({author}), jira_account_id="acct-elene", status="active")
    r = RosterIndex([elene])
    docs = [
        # internal daily written next morning -> kept (belongs to the report day)
        make_email(author=author, external_id="late_am", message_id="m-late_am", folder="internal",
                   event_date=NEXT_DAY,
                   sent_at=datetime(NEXT_DAY.year, NEXT_DAY.month, NEXT_DAY.day, 8, 30)),
        # internal daily written next AFTERNOON -> dropped
        make_email(author=author, external_id="late_pm", message_id="m-late_pm", folder="internal",
                   event_date=NEXT_DAY,
                   sent_at=datetime(NEXT_DAY.year, NEXT_DAY.month, NEXT_DAY.day, 15, 0)),
        # client-facing email next day -> dropped (external is strict report day)
        make_email(author=author, external_id="client_next", message_id="m-client",
                   folder="external", event_date=NEXT_DAY,
                   sent_at=datetime(NEXT_DAY.year, NEXT_DAY.month, NEXT_DAY.day, 8, 30)),
        # Jira updated next day -> dropped (Jira is strict report day)
        make_jira(assignee_account_id="acct-elene", external_id="jira_next", event_date=NEXT_DAY,
                  project_hint="Northerner"),
        # same-day internal daily -> kept
        make_email(author=author, external_id="onday", message_id="m-onday", folder="internal",
                   event_date=REPORT_DATE),
    ]
    result = filter_corpus(docs, r, registry, REPORT_DATE)
    assert _included_ids(result) == {"late_am", "onday"}
    assert _reason(result, "late_pm") == str(ExclusionReason.AFTER_REPORT_DAY)
    assert _reason(result, "client_next") == str(ExclusionReason.AFTER_REPORT_DAY)
    assert _reason(result, "jira_next") == str(ExclusionReason.AFTER_REPORT_DAY)


# --- §7.2.4 Time Logs bucket ticket excluded from budget math -------------------
def test_time_log_bucket_excluded(registry):
    elene = RosterMember(
        id=uuid.uuid4(),
        full_name="Elene Chekurishvili",
        emails=frozenset({"elene.chekurishvili@scandiweb.com"}),
        jira_account_id="acct-elene",
        status="active",
    )
    roster = RosterIndex([elene])
    docs = [
        make_jira(assignee_account_id="acct-elene", issue_type="Time Logs",
                  external_id="timelog", project_hint="Furniture Trader"),
        make_jira(assignee_account_id="acct-elene", issue_type="Task",
                  external_id="realwork", project_hint="Furniture Trader"),
    ]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    assert _included_ids(result) == {"realwork"}
    assert _reason(result, "timelog") == str(ExclusionReason.TIME_LOG_BUCKET)


# --- §7.2.5 Nicokick / Northerner / nicokick/northerner -> one project ----------
def test_nicokick_northerner_resolve_to_one_project(registry):
    variants = ["Nicokick", "Northerner", "nicokick/northerner", "Nicokick / Northerner"]
    resolved = [registry.resolve(v) for v in variants]
    unresolved = [v for v, r in zip(variants, resolved, strict=False) if not r]
    assert all(r is not None for r in resolved), unresolved
    ids = {r.id for r in resolved}
    assert len(ids) == 1
    assert resolved[0].canonical_name == "Northerner"


# --- §11.2 dedup by Message-ID: the model sees each daily once ------------------
def test_dedup_by_message_id(roster, registry):
    mid = "<daily-elene-20260717@scandiweb.com>"
    docs = [
        make_email(author="elene.chekurishvili@scandiweb.com", external_id="copy-felco",
                   message_id=mid, account_id="felco"),
        make_email(author="elene.chekurishvili@scandiweb.com", external_id="copy-reuzel",
                   message_id=mid, account_id="reuzel"),
    ]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    assert len(result.included) == 1
    assert result.duplicate_count == 1
    dup = next(a for a in result.audit if not a.included)
    assert dup.exclusion_reason == "duplicate_of:copy-felco"


# --- §6.2 unmatched project keeps the doc + raises a flag -----------------------
def test_unmatched_project_kept_and_flagged(roster, registry):
    docs = [
        make_email(author="elene.chekurishvili@scandiweb.com", external_id="umich",
                   project_hint="University of Michigan Portal Redesign"),
    ]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    assert "umich" in _included_ids(result)  # never silently dropped
    assert "University of Michigan Portal Redesign" in result.unmatched_projects


# --- §1/§11.1 coverage ratio over ACTIVE roster --------------------------------
def test_coverage_ratio_counts_active_only(roster, registry):
    # one active member reports; coverage = 1 / active_count
    docs = [make_email(author="elene.chekurishvili@scandiweb.com", external_id="one")]
    result = filter_corpus(docs, roster, registry, REPORT_DATE)
    active = roster.active_count
    assert result.coverage_ratio == 1 / active
    assert len(result.silent_person_ids) == active - 1
