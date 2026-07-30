"""Canonical value sets shared by the DB layer and the pure filter code."""

from __future__ import annotations

from enum import StrEnum


class PersonStatus(StrEnum):
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    OUT = "out"


class SendMode(StrEnum):
    NONE = "none"
    SELF = "self"
    DRAFT = "draft"
    SEND = "send"


class RunStatus(StrEnum):
    RUNNING = "running"  # created immediately; the background worker updates it
    OK = "ok"
    FLAGGED = "flagged"
    FAILED = "failed"


class Source(StrEnum):
    GMAIL = "gmail"
    JIRA = "jira"
    FAIRWIND = "fairwind"
    TRANSCRIPT = "transcript"


class SourceMode(StrEnum):
    """Per-pipeline switch (§1). Fairwind is v1; gmail is the built-but-dormant fallback."""

    FAIRWIND = "fairwind"
    GMAIL = "gmail"
    BOTH = "both"


class ExclusionReason(StrEnum):
    NOT_IN_ROSTER = "not_in_roster"
    AFTER_REPORT_DAY = "after_report_day"    # past the report window (channel-dependent)
    BEFORE_REPORT_DAY = "before_report_day"  # older thread message swept in by the export
    TIME_LOG_BUCKET = "time_log_bucket"
    ROSTER_OUT = "roster_out"          # sender is on the roster but status=out/on_leave
    # duplicate_of:{id} is built dynamically


class FlagType(StrEnum):
    BLOCKER = "blocker"
    ESCALATION = "escalation"
    NEEDS_REVIEW = "needs_review"
    UNMATCHED_PROJECT = "unmatched_project"
    INGEST_GAP = "ingest_gap"


# Jira issue types that are time-logging buckets, excluded from budget math (§7.2).
TIME_LOG_BUCKET_TYPES: frozenset[str] = frozenset(
    {"Time Logs", "Time Log", "Timesheet", "Worklog"}
)

UNASSIGNED_PROJECT = "Unassigned"
