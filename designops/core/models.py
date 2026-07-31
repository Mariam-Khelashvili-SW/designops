"""Data model — spec §5 (person, project, pipeline, ingest_batch, pipeline_run,
run_document, artifact, flag) + §11 (account).

Enum-like columns are stored as text; the canonical value sets live in
`designops.core.enums` so both the DB layer and the pure filter code agree.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from designops.core.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class AppState(Base):
    """Small key→JSON store for runtime state that must survive redeploys but isn't a
    domain entity — e.g. the Google OAuth refresh token (so a container restart doesn't
    drop the Gmail connection). NOT for app secrets, which stay in env."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Person(Base):
    """Roster. Identity is email-first; display names are for rendering only (§2.1, §11.3)."""

    __tablename__ = "person"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    emails: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    jira_account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    display_aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    # status ∈ {active, on_leave, out} — single source for §2.7 (Agnese) too
    status: Mapped[str] = mapped_column(String, default="active", nullable=False)
    # inclusive last day of leave; on report_dates after it the person is active again
    leave_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    squad: Mapped[str | None] = mapped_column(String, nullable=True)
    # Dedicated designer: fixed weekly hours count as workload regardless of Jira tasks
    is_dedicated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dedicated_weekly_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    # true once email + jira_account_id verified against the Directory (§9.3, §11.3)
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """Registry — canonicalisation, NOT a filter (§3). aliases[] map the many
    strings people type to one canonical name. Also carries A2 weekly-health
    budget fields when track_weekly_health is set."""

    __tablename__ = "project"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    canonical_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    jira_project_key: Mapped[str | None] = mapped_column(String, nullable=True)
    fairwind_account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # §11.4 — the daily-tracking flag toggled in the config UI (not a separate list)
    track_daily: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # A2 Weekly Project Health & Budget allowlist
    track_weekly_health: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_subtitle: Mapped[str | None] = mapped_column(String, nullable=True)
    signed_design_estimate_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimate_basis: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    jira_scope: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Account(Base):
    """Synced Fairwind directory row (§11). sync_accounts never touches the local
    columns (digest_enabled/aliases/enabled_by/enabled_at/notes)."""

    __tablename__ = "account"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    fairwind_account_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Fairwind's own active flag (sync-owned). "Active accounts" in the UI = this.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    domains: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    jira_project_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, nullable=False
    )
    salesforce_account_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, nullable=False
    )
    notion_space_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    data_availability: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_activity_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # local columns — sync must never overwrite these (§11.1)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    enabled_by: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Pipeline(Base):
    __tablename__ = "pipeline"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_path: Mapped[str] = mapped_column(String, nullable=False)
    schedule_cron: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="Europe/Riga", nullable=False)
    recipients: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    # send_mode ∈ {none, self, draft, send}; default none (§12)
    send_mode: Mapped[str] = mapped_column(String, default="none", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # HARD gate: no delivery of any kind while false (§5, §12.4)
    go_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list[PipelineRun]] = relationship(back_populates="pipeline")


class IngestBatch(Base):
    """Reusable corpus for a report_date. N synthesis runs may share one (§12.2)."""

    __tablename__ = "ingest_batch"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    account_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # status ∈ {running, ok, flagged, failed}
    status: Mapped[str] = mapped_column(String, default="running", nullable=False)
    coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # coverage detail: accounts requested/succeeded/failed (§11.1)
    coverage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline.id"), nullable=False)
    ingest_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingest_batch.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    # status ∈ {ok, flagged, failed}
    status: Mapped[str] = mapped_column(String, default="ok", nullable=False)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # sign-off (§12.4)
    validated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    pipeline: Mapped[Pipeline] = relationship(back_populates="runs")
    documents: Mapped[list[RunDocument]] = relationship(back_populates="run")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="run")
    flags: Mapped[list[Flag]] = relationship(back_populates="run")


class RunDocument(Base):
    """Full audit of what was read AND why dropped (§5). This is the piece that
    makes the system auditable — never skip it."""

    __tablename__ = "run_document"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project.id"), nullable=True)
    included: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # e.g. not_in_roster | after_report_day | time_log_bucket | duplicate_of:{id}
    exclusion_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="documents")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, default="html", nullable=False)  # html | text | json
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # delivery_status ∈ {not_sent, self, draft, sent, blocked_go_live}
    delivery_status: Mapped[str] = mapped_column(String, default="not_sent", nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="artifacts")


class Flag(Base):
    __tablename__ = "flag"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_run.id"), nullable=False)
    # type ∈ {blocker, escalation, needs_review, unmatched_project, ingest_gap}
    type: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project.id"), nullable=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="flags")
    # No uniqueness on (run_id, type, project_id, person_id): a run can raise several
    # unmatched_project flags that all carry null project/person, distinguished by body.
