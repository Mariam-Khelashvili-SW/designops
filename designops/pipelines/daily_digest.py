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
import re
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
from designops.core.projects import (
    collect_mentioned_fairwind_ids,
    enable_accounts_for_jira_keys,
    enable_accounts_for_mentioned_projects,
    ensure_projects_for_jira_keys,
    fairwind_account_ids_for_jira_keys,
    jira_project_keys_from_docs,
    sync_jira_keys_to_projects,
)
from designops.pipelines.email_subjects import email_subject_for_pipeline
from designops.pipelines.filter import FilterResult, filter_corpus
from designops.pipelines.leave_from_vacsick import sync_leave_from_vacsick
from designops.pipelines.render import render_digest
from designops.pipelines.synthesis import synthesize
from designops.pipelines.weekly_availability import (
    week_friday,
    week_monday_on_or_before,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


def _now() -> datetime:
    return datetime.now(UTC)


# --- ingest -------------------------------------------------------------------

def _digest_enabled_account_ids(session: Session) -> list[str]:
    return [
        a.fairwind_account_id
        for a in session.query(Account).filter_by(digest_enabled=True).all()
        if a.fairwind_account_id
    ]


def resolve_daily_fairwind_scope(
    session: Session,
    roster_rows: list,
    report_date: date,
) -> tuple[list[str], list[Document], dict]:
    """Fairwind accounts to export for a daily: Jira ticket projects (+ later mentions).

    Mirrors weekly backlog: query open assigned issues for the roster, map project
    keys → Fairwind accounts, auto-enable those accounts. Does **not** fan out to
    every digest_enabled account — only activity-scoped ones.

    Falls back to digest_enabled when Jira is off or returns no mappable keys.
    """
    from designops.adapters.jira import JiraClient, resolve_roster_account_ids

    settings = get_settings()
    meta: dict = {"fairwind_scope": "jira"}
    jira_docs: list[Document] = []

    if settings.jira_configured:
        query_people = [
            p
            for p in roster_rows
            if effective_status(
                p.status,
                p.leave_until,
                report_date,
                leave_from=getattr(p, "leave_from", None),
            )
            != "on_leave"
        ]
        id_map = resolve_roster_account_ids(
            query_people, persist=True, settings=settings
        )
        session.flush()
        meta["jira_account_ids_resolved"] = len(id_map)
        client = JiraClient(settings)
        if id_map:
            jira_docs = client.search_open_assigned(
                list(id_map.values()),
                window_from=report_date,
                window_to=report_date + timedelta(days=1),
            )
        keys = jira_project_keys_from_docs(jira_docs)
        meta["jira_project_keys"] = sorted(keys)
        meta["jira_docs"] = len(jira_docs)

        # Map unknown keys onto Account/Project (Fairwind jira map + Jira names)
        # *before* choosing which Fairwind accounts to export.
        fw_map: list[dict] = []
        if settings.fairwind_configured:
            try:
                from designops.adapters.fairwind import FairwindClient

                fw_map = FairwindClient(settings).list_jira_projects()
            except Exception as exc:  # noqa: BLE001 — still ensure via Jira names
                meta["fairwind_jira_map_note"] = f"{type(exc).__name__}: {exc}"
        ensure_meta = ensure_projects_for_jira_keys(
            session,
            keys,
            fairwind_jira_projects=fw_map,
            jira_get_project=client.get_project,
        )
        meta["jira_key_ensure"] = {
            "ensured": ensure_meta.get("ensured", 0),
            "linked": ensure_meta.get("linked", []),
            "created": ensure_meta.get("created", []),
        }

        account_ids = sorted(fairwind_account_ids_for_jira_keys(session, keys))
        newly = enable_accounts_for_jira_keys(
            session,
            keys,
            enabled_by=settings.setup_owner_email or "daily-digest-jira",
        )
        meta["accounts_auto_enabled_jira"] = [a.name for a in newly]
        if account_ids:
            meta["fairwind_scope"] = "jira"
            return account_ids, jira_docs, meta
        meta["fairwind_scope_note"] = "jira returned no mappable Fairwind accounts"

    # Offline / empty Jira — keep prior allowlist so fixtures and local runs still work.
    fallback = _digest_enabled_account_ids(session)
    meta["fairwind_scope"] = "digest_enabled_fallback"
    meta.setdefault("jira_project_keys", [])
    meta.setdefault("jira_docs", 0)
    return fallback, jira_docs, meta


def _ingest(
    session: Session,
    report_date: date,
    *,
    reuse: bool = True,
    account_ids: list[str] | None = None,
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
        # Daily passes activity-scoped ids (Jira / mentions). Weekly may omit → all enabled.
        if account_ids is None:
            account_ids = _digest_enabled_account_ids(session)
        else:
            account_ids = [a for a in account_ids if a]
        # §12.2: reuse a corpus already pulled for this date — but ONLY if it covers every
        # requested account. A newly scoped account forces a fresh pull for gaps.
        if reuse:
            cached = load_corpus(settings, report_date)
            covered = set(corpus_account_ids(settings, report_date))
            if cached is not None and set(account_ids) <= covered:
                docs = list(cached)
                coverage = {
                    "source": "fairwind-cached",
                    "accounts_requested": len(account_ids),
                    "exports_succeeded": len(account_ids),
                    "exports_failed": 0,
                    "accounts_cached_pool": len(covered),
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
        coverage["account_ids"] = list(account_ids)
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

    # CRO mailbox — live (not in Fairwind cache). Roster authors = design dailies;
    # everyone else stays beyond_daily signal in the filter.
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
            "status": effective_status(
                r.status,
                r.leave_until,
                report_date,
                leave_from=getattr(r, "leave_from", None),
            ),
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

def _flag_untracked_projects(digest, registry, enabled_ids, enabled_names) -> None:
    """Mark STATUS rows `untracked=True` when a Fairwind export was expected but missing.

    - Unknown project name → untracked (needs an alias / account map).
    - Known project with no Fairwind account (internal boards) → not untracked;
      there is nothing to pull.
    - Known project with Fairwind id not in this run's pulled set → untracked.
    """
    en = {n.lower() for n in enabled_names}
    for row in digest.get("status", []):
        name = str(row.get("project") or "")
        if not name:
            row["untracked"] = False
            continue
        entry = registry.resolve(name)
        if entry is None:
            row["untracked"] = True
            continue
        if not entry.fairwind_account_id:
            row["untracked"] = False
            continue
        tracked = (
            entry.fairwind_account_id in enabled_ids or name.lower() in en
        )
        row["untracked"] = not tracked


_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _working_review_link(link: str | None) -> str | None:
    """Normalize a review link; return None if missing / not usable.

    Tier-1 rule: no link → no review flag. Issue keys become browse URLs when
    JIRA_BASE_URL is configured.
    """
    if link is None:
        return None
    raw = str(link).strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    key = raw.upper()
    if _ISSUE_KEY_RE.match(key):
        base = (get_settings().jira_base_url or "").rstrip("/")
        return f"{base}/browse/{key}" if base else key
    # Bare project keys (DCP1) are not actionable enough for a review deep-link.
    return None


def _enforce_structure(digest, filtered, roster_rows) -> None:
    """Growth-Pulse Tier-1 structure.

    - STATUS / TODAY'S PLANS only from designers who filed a daily (never invent).
    - needs_review rows without a working link are dropped.
    - open_questions require verbatim question + who.
    """
    reported = {p.full_name for p in roster_rows if p.id in filtered.reported_person_ids}
    digest["status"] = [
        s for s in (digest.get("status") or [])
        if s.get("person") in reported and (s.get("line") or "").strip()
    ]
    digest["todays_plans"] = [
        p for p in (digest.get("todays_plans") or [])
        if p.get("person") in reported and (p.get("plan") or "").strip()
    ]
    kept_review = []
    for r in digest.get("needs_review") or []:
        if not (r.get("item") or "").strip():
            continue
        link = _working_review_link(r.get("link"))
        if not link:
            continue
        r = dict(r)
        r["link"] = link
        kept_review.append(r)
    digest["needs_review"] = kept_review
    digest["open_questions"] = [
        q for q in (digest.get("open_questions") or [])
        if (q.get("question") or "").strip() and (q.get("who") or "").strip()
    ]
    # One plan row per person (keep first non-empty if the model duplicated).
    seen_plans: set[str] = set()
    deduped_plans = []
    for p in digest["todays_plans"]:
        name = p.get("person") or ""
        if name in seen_plans:
            continue
        seen_plans.add(name)
        deduped_plans.append(p)
    digest["todays_plans"] = deduped_plans


def _leave_context(person) -> str:
    """Human leave note for the availability section."""
    leave_from = getattr(person, "leave_from", None)
    leave_until = person.leave_until
    if leave_from and leave_until:
        if leave_from == leave_until:
            return f"On leave {leave_until.isoformat()}."
        return f"On leave {leave_from.isoformat()}–{leave_until.isoformat()}."
    if leave_until:
        return f"On leave until {leave_until.isoformat()}."
    return "On leave."


def next_working_day(d: date) -> date:
    """Calendar day after `d`, skipping Saturday/Sunday."""
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def _person_on_leave_day(person, day: date) -> bool:
    return (
        effective_status(
            person.status,
            person.leave_until,
            day,
            leave_from=getattr(person, "leave_from", None),
        )
        == "on_leave"
    )


def _annotate_upcoming_leave(digest, roster_rows, report_date: date) -> None:
    """If someone is on leave the *next* working day (but not today), say so in plans.

    Uses Person leave window (Config + Tempo VACSICK sync). Does not invent leave —
    only surfaces what is already on the roster. Allowed even when they filed no daily.
    """
    nxt = next_working_day(report_date)
    # e.g. "Tue 5 Aug"
    nxt_label = nxt.strftime("%a %-d %b").replace(" 0", " ")
    plans = list(digest.get("todays_plans") or [])
    by_name = {p.get("person"): p for p in plans if p.get("person")}

    for person in roster_rows:
        if _person_on_leave_day(person, report_date):
            continue
        if not _person_on_leave_day(person, nxt):
            continue
        leave_until = person.leave_until
        if leave_until and leave_until > nxt:
            note = f"On leave from {nxt_label} (through {leave_until.strftime('%-d %b')})."
        else:
            note = f"On leave {nxt_label} (next working day)."
        existing = by_name.get(person.full_name)
        if existing is not None:
            plan = (existing.get("plan") or "").strip()
            if "on leave" in plan.lower():
                continue
            existing["plan"] = f"{plan} · {note}" if plan else note
        else:
            row = {"person": person.full_name, "plan": note, "leave_upcoming": True}
            plans.append(row)
            by_name[person.full_name] = row

    plans.sort(key=lambda p: (not p.get("leave_upcoming"), p.get("person") or ""))
    digest["todays_plans"] = plans


def _reconcile_availability(digest, filtered, roster_rows, report_date) -> None:
    """Overwrite `no_report` + Growth-Pulse KPIs from the filter (authoritative).

    Silent designers get status=no_report with no subtitle — the chip is enough.
    On-leave keeps a short date note (not counted in the no_report KPI).
    """
    people = {p.id: p for p in roster_rows}
    rows = []
    for pid in filtered.silent_person_ids:
        p = people.get(pid)
        if p:
            rows.append({"name": p.full_name, "status": "no_report", "context": None})
    for p in roster_rows:
        if (
            effective_status(
                p.status,
                p.leave_until,
                report_date,
                leave_from=getattr(p, "leave_from", None),
            )
            == "on_leave"
        ):
            rows.append({
                "name": p.full_name,
                "status": "on_leave",
                "context": _leave_context(p),
            })
    rows.sort(key=lambda r: (r["status"] != "no_report", r["name"]))
    digest["no_report"] = rows

    review = digest.get("needs_review") or []
    g = digest.setdefault("at_a_glance", {})
    g["active"] = len(filtered.reported_person_ids)
    g["need_review"] = len(review)
    g["blocked"] = sum(1 for r in review if r.get("blocked"))
    g["no_report"] = sum(1 for r in rows if r["status"] == "no_report")
    # Back-compat aliases for older templates / fixtures
    g["reported"] = g["active"]
    g["escalations"] = g["need_review"]


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
    # All DB designers except former members (`out`). `on_leave` stays in the roster
    # and shows under availability — never counted as a missing daily.
    roster_rows = [p for p in session.query(Person).all() if p.status != "out"]
    project_rows = session.query(Project).all()

    # Tempo VACSICK (≥8h/day) → Person.on_leave before filter/coverage, same as A3.
    week_mon = week_monday_on_or_before(report_date)
    leave_cov = sync_leave_from_vacsick(
        roster_rows,
        week_monday=week_mon,
        week_friday=week_friday(week_mon),
    )
    if leave_cov.get("updated_names"):
        session.flush()

    roster = RosterIndex.from_rows(roster_rows, report_date)
    # Registry rebuilt after ingest once Jira keys are synced from accounts/corpus.
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
        # Only Fairwind-export accounts tied to roster Jira tickets (mentions add more below).
        scoped_ids, jira_docs, scope_meta = resolve_daily_fairwind_scope(
            session, roster_rows, report_date
        )
        documents, coverage = _ingest(
            session, report_date, reuse=reuse_ingest, account_ids=scoped_ids
        )
        coverage["vacsick_leave"] = leave_cov
        coverage.update(scope_meta)
        pulled_ids: set[str] = set(scoped_ids)

        # Merge direct Jira issues so ticket context isn't lost when Fairwind is scoped thin.
        seen_ext = {
            (d.external_id or "").strip()
            for d in documents
            if getattr(d, "external_id", None)
        }
        for jd in jira_docs:
            eid = (jd.external_id or "").strip()
            if eid and eid not in seen_ext:
                documents.append(jd)
                seen_ext.add(eid)

        # Fairwind already knows account→Jira keys; copy them onto Project (+ learn
        # any new board keys seen on this corpus) so DCP1/JYDP/AAD resolve automatically.
        key_sync = sync_jira_keys_to_projects(session, documents)
        coverage["jira_key_sync"] = key_sync
        project_rows = session.query(Project).all()
        account_rows = session.query(Account).all()
        registry = ProjectRegistry.from_rows(project_rows, accounts=account_rows)

        # Second pass: projects named in dailies that weren't in the Jira set → pull those too.
        mentioned_fw = collect_mentioned_fairwind_ids(documents, registry)
        settings = get_settings()
        newly_enabled = enable_accounts_for_mentioned_projects(
            session,
            fairwind_account_ids=mentioned_fw,
            enabled_by=settings.setup_owner_email or "daily-digest-auto",
        )
        coverage["accounts_auto_enabled"] = [a.name for a in newly_enabled]
        need_ids = sorted(mentioned_fw - pulled_ids)
        if need_ids and settings.fairwind_configured:
            try:
                client = FairwindClient(settings)
                extra_docs, extra_cov = client.prepare_corpus(
                    need_ids,
                    report_date,
                    window_end=report_date + timedelta(days=1),
                )
                if extra_docs:
                    documents = list(documents) + list(extra_docs)
                pulled_ids |= set(need_ids)
                save_corpus(
                    settings, report_date, documents, account_ids=sorted(pulled_ids)
                )
                coverage["mention_exports_succeeded"] = extra_cov.get(
                    "exports_succeeded", 0
                )
                coverage["mention_exports_failed"] = extra_cov.get("exports_failed", 0)
                coverage["mention_account_ids"] = need_ids
                if extra_cov.get("exports_failed"):
                    coverage["incomplete"] = True
                key_sync2 = sync_jira_keys_to_projects(session, extra_docs)
                coverage["jira_key_sync_mentions"] = key_sync2
                project_rows = session.query(Project).all()
                account_rows = session.query(Account).all()
                registry = ProjectRegistry.from_rows(project_rows, accounts=account_rows)
            except Exception as exc:  # noqa: BLE001 — enable still sticks; gap noted
                coverage["mention_export_note"] = f"{type(exc).__name__}: {exc}"

        coverage["accounts_pulled"] = sorted(pulled_ids)
        coverage["accounts_requested"] = len(pulled_ids)

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
        # "Tracked" this run = accounts we actually exported (Jira + mention scope),
        # not the full historical digest_enabled list.
        pulled_accts = [
            a
            for a in session.query(Account).all()
            if a.fairwind_account_id and a.fairwind_account_id in pulled_ids
        ]
        enabled_ids = set(pulled_ids)
        tracked = sorted(
            {a.name for a in pulled_accts}
            | {
                p.canonical_name
                for p in project_rows
                if p.fairwind_account_id in enabled_ids
            }
        )
        digest, synth = _synthesize(
            filtered, report_date, roster_rows, project_rows, tracked_accounts=tracked
        )
        # Growth-Pulse: enforce status/plans from dailies only; set availability + KPIs.
        _enforce_structure(digest, filtered, roster_rows)
        # Safety net: status projects still marked untracked → enable + pull next time via mentions.
        status_names = {
            str(s.get("project") or "").strip()
            for s in (digest.get("status") or [])
            if s.get("project")
        }
        more = enable_accounts_for_mentioned_projects(
            session,
            project_names=status_names,
            enabled_by=get_settings().setup_owner_email or "daily-digest-auto",
        )
        if more:
            coverage.setdefault("accounts_auto_enabled", [])
            coverage["accounts_auto_enabled"] = sorted(
                set(coverage["accounts_auto_enabled"]) | {a.name for a in more}
            )
            for a in more:
                if a.fairwind_account_id:
                    enabled_ids.add(a.fairwind_account_id)
        _flag_untracked_projects(
            digest,
            registry,
            enabled_ids,
            {a.name for a in pulled_accts}
            | {a.name for a in more},
        )
        _reconcile_availability(digest, filtered, roster_rows, report_date)
        _annotate_upcoming_leave(digest, roster_rows, report_date)
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
