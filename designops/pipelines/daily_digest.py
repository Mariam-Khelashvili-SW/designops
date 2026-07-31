"""A1 Daily Ops Digest — DB-backed orchestration (§6, §12).

`run_daily_digest(session, report_date, ...)` runs the six stages and persists the full
audit: ingest_batch, pipeline_run, run_document (every read + verdict), artifact, flags.

Three ingest/synthesis modes so validation works before creds land (§12):
  * ingest: live Fairwind fan-out if configured, else the offline corpus fixture.
  * synthesize: real LLM if ANTHROPIC_API_KEY, else a recorded golden digest fixture
    (marked in the run note) so the artifact viewer is exercisable today.
Delivery is always gated by go_live in the delivery adapter (§12.4).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from designops.adapters.delivery import deliver
from designops.adapters.documents import Document
from designops.adapters.fairwind import (
    FairwindClient,
    corpus_account_ids,
    load_corpus,
    load_fixture_corpus,
    save_corpus,
)
from designops.core.config import get_settings
from designops.core.enums import FlagType, RunStatus
from designops.core.identity import RosterIndex, effective_status
from designops.core.models import (
    Account,
    Artifact,
    Flag,
    IngestBatch,
    Person,
    Pipeline,
    PipelineRun,
    Project,
    RunDocument,
)
from designops.core.registry import ProjectRegistry
from designops.pipelines.email_subjects import email_subject_for_pipeline
from designops.pipelines.filter import FilterResult, filter_corpus
from designops.pipelines.render import render_digest
from designops.pipelines.synthesis import synthesize

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


def _now() -> datetime:
    return datetime.now(UTC)


# --- ingest -------------------------------------------------------------------

def _ingest(
    session: Session, report_date: date, *, reuse: bool = True
) -> tuple[list[Document], dict]:
    settings = get_settings()
    fixture = FIXTURES / report_date.isoformat() / "corpus.json"
    docs: list[Document] = []
    coverage: dict = {
        "accounts_requested": 0,
        "exports_succeeded": 0,
        "exports_failed": 0,
    }
    if settings.fairwind_configured:
        # allowlist = accounts enabled on the Accounts screen (§11); content relevance
        # inside each export is the model's call, this only scopes what's fetched.
        account_ids = [
            a.fairwind_account_id
            for a in session.query(Account).filter_by(digest_enabled=True).all()
            if a.fairwind_account_id
        ]
        # §12.2: reuse a corpus already pulled for this date — but ONLY if it covers every
        # currently-enabled account. Enabling a new account (or unchecking reuse) forces a
        # fresh pull, so a newly-enabled account can never be silently missing.
        if reuse:
            cached = load_corpus(settings, report_date)
            covered = set(corpus_account_ids(settings, report_date))
            if cached is not None and set(account_ids) <= covered:
                docs = list(cached)
                coverage = {
                    "source": "fairwind-cached",
                    "accounts_requested": len(covered),
                    "exports_succeeded": len(covered),
                    "exports_failed": 0,
                }
            else:
                reuse = False
        if not reuse or coverage.get("source") != "fairwind-cached":
            client = FairwindClient(settings)
            # Export report_date + the next day so a daily written the next morning is caught;
            # the filter enforces channel-aware temporal scope on the 2-day corpus.
            docs, coverage = client.prepare_corpus(
                account_ids, report_date, window_end=report_date + timedelta(days=1)
            )
            save_corpus(settings, report_date, docs, account_ids=account_ids)
            coverage["source"] = "fairwind"
    elif fixture.exists():
        docs = load_fixture_corpus(fixture)
        coverage = {
            "source": "fixture",
            "accounts_requested": 0,
            "exports_succeeded": 0,
            "exports_failed": 0,
        }
    else:
        coverage = {
            "source": "empty",
            "accounts_requested": 0,
            "exports_succeeded": 0,
            "exports_failed": 0,
        }

    # CRO mailbox — always live (not in Fairwind cache); beyond_daily only.
    cro_docs, cro_meta = _ingest_cro(report_date, settings)
    if cro_docs:
        docs = list(docs) + cro_docs
    coverage.update(cro_meta)
    return docs, coverage


def _ingest_cro(report_date: date, settings) -> tuple[list[Document], dict]:
    """Pull report-day cro@ messages when the CRO Google grant is connected."""
    from designops.adapters import google_oauth
    from designops.adapters.gmail import list_cro_documents

    meta = {
        "cro_mailbox": settings.cro_mailbox_email,
        "cro_connected": False,
        "cro_messages": 0,
        "cro_note": None,
    }
    if not google_oauth.is_cro_connected(settings):
        meta["cro_note"] = "CRO mailbox not connected"
        return [], meta
    meta["cro_connected"] = True
    try:
        # Gmail before: is exclusive → before = report_date + 1 covers the report day.
        cro_docs = list_cro_documents(
            after=report_date,
            before=report_date + timedelta(days=1),
            max_results=40,
            settings=settings,
        )
        # Keep only report_day events (query is date-bounded; double-check).
        cro_docs = [d for d in cro_docs if d.event_date == report_date]
        meta["cro_messages"] = len(cro_docs)
        return cro_docs, meta
    except Exception as e:  # noqa: BLE001 — gap, not a failed digest
        meta["cro_note"] = f"{type(e).__name__}: {e}"
        meta["cro_messages"] = 0
        return [], meta


# --- synthesize ---------------------------------------------------------------

def _synthesize(
    filtered: FilterResult, report_date: date, roster_rows, project_rows,
    tracked_accounts: list[str] | None = None,
) -> tuple[dict, dict]:
    """Return (digest_json, synth_meta)."""
    settings = get_settings()
    roster_names = [
        {
            "full_name": r.full_name,
            "status": effective_status(r.status, r.leave_until, report_date),
        }
        for r in roster_rows
    ]
    project_names = [
        {"canonical_name": p.canonical_name, "aliases": p.aliases} for p in project_rows
    ]
    if settings.anthropic_configured:
        digest, result = synthesize(
            filtered, report_date, roster_names, project_names,
            tracked_accounts=tracked_accounts,
        )
        return digest, {
            "mode": "llm", "model": result.model,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cost_usd": float(result.cost_usd),
        }
    recorded = FIXTURES / report_date.isoformat() / "expected_digest.json"
    if recorded.exists():
        return json.loads(recorded.read_text()), {
            "mode": "recorded", "model": None, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0,
            "note": "No ANTHROPIC_API_KEY — used recorded golden digest for validation.",
        }
    raise RuntimeError(
        "Cannot synthesize: no ANTHROPIC_API_KEY and no recorded digest fixture for "
        f"{report_date}."
    )


# --- orchestration ------------------------------------------------------------

_NOTE_FIELDS = ("blocker", "waiting", "escalation", "heads_up", "agent_note", "jira")


def _flag_untracked_projects(digest, registry, enabled_ids, enabled_names) -> None:
    """Mark each project group `untracked=True` when no export was pulled for it — i.e. it
    doesn't map to an enabled account. Deterministic (code), so the inline flag is reliable
    even when the model's own judgement isn't."""
    en = {n.lower() for n in enabled_names}
    for proj in digest.get("projects", []):
        name = str(proj.get("name", ""))
        entry = registry.resolve(name)
        tracked = bool(entry and entry.fairwind_account_id in enabled_ids) or name.lower() in en
        proj["untracked"] = not tracked


def _enforce_structure(digest, filtered, roster_rows) -> None:
    """Enforce the per-person structure (Liana, 22 Jul): `done`/`next` come ONLY from a
    person's own daily. For anyone who filed no daily, null those out — they may still be
    listed under a project IF a real Jira/external note surfaced, but never with a
    fabricated Done. A person entry with nothing left, and a project with no people and no
    beyond-daily, are removed."""
    reported = {p.full_name for p in roster_rows if p.id in filtered.reported_person_ids}
    kept_projects = []
    for proj in digest.get("projects", []):
        people = []
        for pp in proj.get("people", []):
            if pp.get("name") not in reported:
                pp["done"] = None       # no daily → no summary of their day
                pp["next"] = None
            if pp.get("done") or pp.get("next") or any(pp.get(f) for f in _NOTE_FIELDS):
                people.append(pp)
        proj["people"] = people
        if people or proj.get("beyond_daily"):
            kept_projects.append(proj)
    digest["projects"] = kept_projects
    g = digest.setdefault("at_a_glance", {})
    allp = [pp for p in kept_projects for pp in p.get("people", [])]
    g["blocked"] = sum(1 for pp in allp if pp.get("blocker"))
    g["escalations"] = sum(1 for pp in allp if pp.get("escalation"))


def _reconcile_availability(digest, filtered, roster_rows, report_date) -> None:
    """Overwrite `no_report` + its KPI from the filter (authoritative), keeping the model's
    per-person context where it gave one. Silent = active designer with no daily; plus the
    on-leave designers. `out` designers were never in the roster."""
    people = {p.id: p for p in roster_rows}
    ctx = {nr.get("name", ""): nr.get("context", "") for nr in digest.get("no_report", [])}
    rows = []
    for pid in filtered.silent_person_ids:
        p = people.get(pid)
        if p:
            rows.append({"name": p.full_name, "status": "no_report",
                         "context": ctx.get(p.full_name) or "No daily in the corpus."})
    for p in roster_rows:
        if effective_status(p.status, p.leave_until, report_date) == "on_leave":
            note = ctx.get(p.full_name) or (
                f"On leave{(' until ' + p.leave_until.isoformat()) if p.leave_until else ''}."
            )
            rows.append({"name": p.full_name, "status": "on_leave", "context": note})
    rows.sort(key=lambda r: (r["status"] != "no_report", r["name"]))
    digest["no_report"] = rows
    g = digest.setdefault("at_a_glance", {})
    g["no_report"] = sum(1 for r in rows if r["status"] == "no_report")
    g["reported"] = len(filtered.reported_person_ids)  # designers who filed a daily


def create_pending_run(session: Session, report_date: date) -> PipelineRun:
    """Create the run row immediately with status=running, so the UI can show it and
    the user can open it while the heavy work happens in the background."""
    pipeline = session.query(Pipeline).filter_by(key="daily-digest").one()
    run = PipelineRun(
        pipeline_id=pipeline.id,
        report_date=report_date,
        started_at=_now(),
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


def execute_run(
    session: Session,
    run: PipelineRun,
    *,
    reuse_ingest: bool = True,
    send_mode_override: str | None = None,
) -> PipelineRun:
    """Run the heavy stages and update `run` in place to a terminal status. Safe to call
    from a background thread with its own session (fetch the run there first)."""
    report_date = run.report_date
    pipeline = session.get(Pipeline, run.pipeline_id)
    # `out` = former team member — excluded from the digest entirely (never shown).
    # `on_leave` stays in the roster and shows as on-leave in the availability section.
    roster_rows = [p for p in session.query(Person).all() if p.status != "out"]
    project_rows = session.query(Project).all()
    roster = RosterIndex.from_rows(roster_rows, report_date)
    registry = ProjectRegistry.from_rows(project_rows)

    filtered = None
    try:
        # --- ingest batch (reusable per report_date, §12.2) ---
        batch = None
        if reuse_ingest:
            batch = (
                session.query(IngestBatch)
                .filter_by(report_date=report_date, status="ok")
                .order_by(IngestBatch.finished_at.desc())
                .first()
            )
        documents, coverage = _ingest(session, report_date, reuse=reuse_ingest)
        if batch is None:
            batch = IngestBatch(
                report_date=report_date,
                account_ids=coverage.get("failed_accounts", []) or [],
                started_at=_now(),
                finished_at=_now(),
                status="ok",
                doc_count=len(documents),
                coverage=coverage,
            )
            session.add(batch)
            session.flush()
        run.ingest_batch_id = batch.id

        account_names = {
            a.fairwind_account_id: a.name
            for a in session.query(Account).all()
            if a.fairwind_account_id
        }
        filtered = filter_corpus(
            documents, roster, registry, report_date, account_names=account_names
        )
        # "Tracked" = both the enabled account names AND the canonical project names whose
        # account is enabled — designers mention project names (Enmedify, UG), which don't
        # always equal the account name (MediCrops, The Universal Group).
        enabled_accts = session.query(Account).filter_by(digest_enabled=True).all()
        enabled_ids = {a.fairwind_account_id for a in enabled_accts}
        tracked = sorted(
            {a.name for a in enabled_accts}
            | {p.canonical_name for p in project_rows if p.fairwind_account_id in enabled_ids}
        )
        digest, synth = _synthesize(
            filtered, report_date, roster_rows, project_rows, tracked_accounts=tracked
        )
        # Enforce the per-person structure (done/next only from a daily; drop fabricated
        # entries) then set the availability + KPIs authoritatively from the filter.
        _enforce_structure(digest, filtered, roster_rows)
        _flag_untracked_projects(
            digest, registry, enabled_ids, {a.name for a in enabled_accts}
        )
        _reconcile_availability(digest, filtered, roster_rows, report_date)
        incomplete = coverage.get("exports_failed", 0) > 0
        html = render_digest(
            digest, report_date, sample=True, coverage={**coverage, "incomplete": incomplete}
        )
    except Exception as e:  # fail loudly, never a partial digest (§6.6)
        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = _now()
        session.add(run)
        session.flush()
        if filtered is not None:
            _persist_documents(session, run, filtered)
        return run

    # --- coverage / status ---
    floor = get_settings().min_coverage
    below_floor = filtered.coverage_ratio < floor
    run.status = (
        RunStatus.FLAGGED
        if (below_floor or coverage.get("exports_failed", 0) > 0)
        else RunStatus.OK
    )

    # --- deliver (gated) ---
    delivery = deliver(
        go_live=pipeline.go_live,
        send_mode=send_mode_override or pipeline.send_mode,
        html=html,
        recipients=pipeline.recipients,
        subject=email_subject_for_pipeline("daily-digest", report_date),
        setup_owner_email=get_settings().setup_owner_email,
    )

    # --- log ---
    run.finished_at = _now()
    run.counts = {**filtered.counts(), "coverage": coverage}
    run.input_tokens = synth["input_tokens"]
    run.output_tokens = synth["output_tokens"]
    run.cost_usd = synth["cost_usd"]
    run.skill_version = synth.get("model") or synth["mode"]
    run.note = synth.get("note")
    session.add(run)
    session.flush()

    _persist_documents(session, run, filtered)
    session.add(
        Artifact(run_id=run.id, kind="html", content=html, delivery_status=delivery.status,
                 delivered_at=_now() if delivery.status in ("self", "draft", "sent") else None,
                 message_id=delivery.message_id)
    )
    _persist_flags(session, run, filtered, below_floor)
    return run


def run_daily_digest(
    session: Session,
    report_date: date,
    *,
    reuse_ingest: bool = True,
    send_mode_override: str | None = None,
) -> PipelineRun:
    """Synchronous end-to-end run (scripts, tests). Creates the run then executes it."""
    run = create_pending_run(session, report_date)
    return execute_run(
        session, run, reuse_ingest=reuse_ingest, send_mode_override=send_mode_override
    )


def _persist_documents(session: Session, run: PipelineRun, filtered: FilterResult) -> None:
    for a in filtered.audit:
        session.add(
            RunDocument(
                run_id=run.id, source=a.source, external_id=a.external_id,
                event_date=a.event_date, person_id=a.person_id, project_id=a.project_id,
                included=a.included, exclusion_reason=a.exclusion_reason, title=a.title,
            )
        )


def _persist_flags(
    session: Session, run: PipelineRun, filtered: FilterResult, below_floor: bool
) -> None:
    if below_floor:
        session.add(
            Flag(run_id=run.id, type=FlagType.INGEST_GAP,
                 body=f"Roster coverage {filtered.coverage_ratio:.0%} below floor "
                      f"{get_settings().min_coverage:.0%}.")
        )
    for raw_string in filtered.unmatched_projects:
        session.add(
            Flag(run_id=run.id, type=FlagType.UNMATCHED_PROJECT,
                 body=f"Unresolved project string: {raw_string!r} — map it to an account "
                      f"or add an alias.")
        )
