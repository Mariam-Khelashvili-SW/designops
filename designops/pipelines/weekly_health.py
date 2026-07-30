"""A2 Weekly Project Health & Budget — DB-backed orchestration.

Snapshot brief for Olga, dated the day it is generated: full-history Jira by
project key (design-scoped burn) + Fairwind client comms for the trailing
7 days. LLM writes health/highlights/actions only.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from designops.adapters.delivery import deliver
from designops.adapters.documents import Document
from designops.adapters.fairwind import FairwindClient, FairwindError
from designops.adapters.jira import JiraClient
from designops.adapters.llm import LLMClient, parse_digest_json
from designops.core.config import get_settings
from designops.core.enums import FlagType, RunStatus, SendMode
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
from designops.pipelines.email_subjects import email_subject_for_pipeline
from designops.pipelines.render import render_weekly_health
from designops.pipelines.weekly_health_commercial import (
    fetch_salesforce_commercial,
    summarize_live_project_meta,
)
from designops.pipelines.weekly_health_invoices import (
    fetch_week_client_comms,
    format_comms_excerpt,
    summarize_ux_invoiced,
)
from designops.pipelines.weekly_health_math import (
    apply_jira_scope,
    build_project_burn,
    design_roster_emails,
    doc_to_ticket,
    epic_subtitle,
    fmt_hours,
    glance_kpis,
    in_design_scope,
    project_jira_links,
    rag_sort_key,
)
from designops.pipelines.weekly_health_meetings import (
    call_dates_for_domains,
    design_participant_emails,
    empty_call_dates,
)

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "weekly-health.md"
PIPELINE_KEY = "weekly-health"


def _now() -> datetime:
    return datetime.now(UTC)


def _fallback_health(card: dict) -> dict:
    mismatches = []
    for t in card.get("tickets") or []:
        status = (t.get("status") or "").lower()
        logged = float(t.get("logged") or 0)
        if status in {"new", "backlog", "to do"} and logged > 0:
            mismatches.append(
                f"{t.get('key')} is {t.get('status')} with {logged:g}h logged"
            )
        if status == "in progress" and logged <= 0:
            mismatches.append(f"{t.get('key')} is In Progress with 0h logged")
    if mismatches:
        return {
            "clean": False,
            "text": "<b>Status/time mismatch:</b> " + "; ".join(mismatches[:3]) + ".",
        }
    return {
        "clean": True,
        "text": "<b>Clean.</b> No obvious status/time mismatches in the scoped tickets.",
    }


def _fallback_actions(projects: list[dict]) -> list[dict]:
    actions = []
    for p in projects:
        if p.get("pending"):
            continue
        for a in p.get("aged_client_action") or []:
            actions.append(
                {
                    "text": (
                        f"Nudge {a.get('key')} — waiting for the client (in Client "
                        f"Action) since {a.get('client_action_since')} "
                        f"({a.get('working_days_in_status')} working days)."
                    ),
                    "project": p["display_name"],
                }
            )
        health = p.get("health") or {}
        if health.get("clean") is False and health.get("text"):
            actions.append(
                {
                    "text": f"Fix hygiene on {p['display_name']}: "
                    + str(health["text"]).replace("<b>", "").replace("</b>", ""),
                    "project": p["display_name"],
                }
            )
        for h in p.get("highlights") or []:
            if h.get("text"):
                actions.append(
                    {
                        "text": h["text"],
                        "project": p["display_name"],
                        "agent_note": h.get("agent_note"),
                    }
                )
    return actions[:6]


def _synthesize(
    cards: list[dict],
    *,
    as_of: date,
    comms_from: date,
    comms_by_project: dict[str, str],
) -> tuple[list[dict], list[dict], dict]:
    settings = get_settings()
    skill = _SKILL.read_text(encoding="utf-8")
    system = (
        skill.replace("{as_of}", as_of.isoformat())
        .replace("{comms_from}", comms_from.isoformat())
    )

    chunks = [
        f"Snapshot date: {as_of.strftime('%a %d %b %Y')} — burn and ticket states are live as of this day.",
        f"CLIENT_COMMS cover the last 7 days ({comms_from.isoformat()} to {as_of.isoformat()}).",
        "Fill health, verdict, highlights, and roll-up actions. Numbers are already set.",
        "",
    ]
    for c in cards:
        if c.get("pending"):
            continue
        chunks.append(f"### {c['display_name']}")
        chunks.append(
            f"Signed={c.get('signed_estimate_display')}; logged={c.get('logged_h')}h; "
            f"status={c.get('rag_label')}; over_est={c.get('over_est_count')}; "
            f"client_action={c.get('client_action_count')}; "
            f"last_call={c.get('last_call_display') or 'n/a'}; "
            f"next_call={c.get('next_call_display') or 'n/a'}"
        )
        chunks.append(f"AGREEMENT: {json.dumps(c.get('_agreement') or {})}")
        chunks.append("SCOPE_TICKETS:")
        for t in (c.get("tickets") or [])[:40]:
            days = t.get("days_in_status")
            days_s = f"{days}d" if days is not None else "?"
            chunks.append(
                f"- {t.get('key')}: {t.get('summary')} | status={t.get('status')} | "
                f"est={t.get('est')} | logged={t.get('logged')} | owner={t.get('owner')} | "
                f"days_in_status={days_s} | client_since={t.get('client_action_since')}"
            )
        chunks.append("CLIENT_COMMS:")
        chunks.append(comms_by_project.get(c["display_name"]) or "(none)")
        chunks.append("")
    user = "\n".join(chunks)

    # Code-side health fallback first
    for c in cards:
        if c.get("pending"):
            continue
        c["health"] = _fallback_health(c)

    if not settings.anthropic_configured:
        actions = _fallback_actions(cards)
        for c in cards:
            c.pop("_agreement", None)
        return cards, actions, {
            "mode": "fallback",
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "note": "No ANTHROPIC_API_KEY — code-side health + ageing actions.",
        }

    result = LLMClient(settings).synthesize(system=system, user_content=user, max_tokens=7000)
    parsed = parse_digest_json(result.text)
    by_name = {
        str(x.get("name", "")).strip(): x
        for x in (parsed.get("projects") or [])
        if isinstance(x, dict)
    }
    for c in cards:
        if c.get("pending"):
            continue
        note = by_name.get(c["display_name"])
        if not note:
            continue
        if note.get("verdict"):
            c["verdict"] = str(note["verdict"]).strip()
        h = note.get("health")
        if isinstance(h, dict) and h.get("text"):
            c["health"] = {
                "clean": bool(h.get("clean", True)),
                "text": str(h["text"]).strip(),
            }
        highlights = []
        for item in note.get("highlights") or []:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            highlights.append(
                {
                    "severity": item.get("severity") or "client_ageing",
                    "label": item.get("label") or "Client",
                    "text": str(item["text"]).strip(),
                    "quote": item.get("quote"),
                    "source": item.get("source"),
                    "agent_note": item.get("agent_note"),
                }
            )
        c["highlights"] = highlights[:5]
        # Hygiene flag downgrades a clean-looking project to Watch
        if c["health"].get("clean") is False and c.get("rag") == "g":
            c["rag"] = "a"
            c["rag_label"] = "Watch"

    actions = []
    for a in parsed.get("actions") or []:
        if isinstance(a, dict) and a.get("text"):
            actions.append(
                {
                    "text": str(a["text"]).strip(),
                    "project": str(a.get("project") or "").strip(),
                    "agent_note": a.get("agent_note"),
                    "evidence": (str(a["evidence"]).strip() or None)
                    if a.get("evidence")
                    else None,
                }
            )
    if not actions:
        actions = _fallback_actions(cards)

    for c in cards:
        c.pop("_agreement", None)

    # Refresh action_items count after LLM
    return cards, actions[:6], {
        "mode": "llm",
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": float(result.cost_usd),
    }


def _commercial_for_project(
    proj: Project,
    *,
    fw_client: FairwindClient | None,
    as_of: date,
    coverage: dict,
    reuse: bool = True,
) -> tuple[dict, dict]:
    """Fairwind invoices + agreement/opportunity meta (live or cached)."""
    empty_meta = {
        "subtitle": "",
        "agreement": {},
        "signed_design_estimate_h": None,
        "source": None,
    }
    unavailable_invoice = {
        "invoiced_label": "n/a",
        "invoiced_muted": True,
        "invoiced_sub": "Fairwind unavailable",
        "invoice_notes": None,
        "ux_invoice_lines": [],
        "ux_invoiced_total": 0.0,
        "_invoice_gap": True,
    }
    no_account_invoice = {
        "invoiced_label": "n/a",
        "invoiced_muted": True,
        "invoiced_sub": "no Fairwind account linked",
        "invoice_notes": None,
        "ux_invoice_lines": [],
        "ux_invoiced_total": 0.0,
        "_invoice_gap": False,
    }
    account_id = (proj.fairwind_account_id or "").strip()
    if not account_id:
        return no_account_invoice, empty_meta
    if not fw_client:
        return unavailable_invoice, empty_meta
    t0 = time.perf_counter()
    try:
        commercial, source = fetch_salesforce_commercial(
            fw_client, account_id, date_to=as_of, reuse=reuse
        )
        invoices = commercial.get("invoices") or []
        summary = summarize_ux_invoiced(invoices)
        meta = summarize_live_project_meta(
            commercial.get("agreements") or [],
            commercial.get("opportunities") or [],
            jira_key=proj.jira_project_key,
        )
        coverage.setdefault("invoices_by_project", {})[proj.canonical_name] = {
            "account_id": account_id,
            "invoice_count": len(invoices),
            "ux_invoice_lines": len(summary.get("ux_invoice_lines") or []),
            "ux_invoiced_total": summary.get("ux_invoiced_total"),
            "agreements": len(commercial.get("agreements") or []),
            "opportunities": len(commercial.get("opportunities") or []),
            "subtitle": meta.get("subtitle"),
            "signed_design_estimate_h": meta.get("signed_design_estimate_h"),
            "source": source,
            "seconds": round(time.perf_counter() - t0, 2),
        }
        summary["_invoice_gap"] = False
        return summary, meta
    except Exception as e:  # noqa: BLE001 — one account gap, not a failed run
        coverage.setdefault("invoice_failures", []).append(
            {
                "project": proj.canonical_name,
                "error": str(e),
                "seconds": round(time.perf_counter() - t0, 2),
            }
        )
        return {
            "invoiced_label": "n/a",
            "invoiced_muted": True,
            "invoiced_sub": "Fairwind invoice pull failed",
            "invoice_notes": None,
            "ux_invoice_lines": [],
            "ux_invoiced_total": 0.0,
            "_invoice_gap": True,
        }, empty_meta


def _comms_for_project(
    proj: Project,
    *,
    fw_client: FairwindClient | None,
    date_from: date,
    date_to: date,
    coverage: dict,
    reuse: bool = True,
) -> str:
    """Fairwind external emails + transcripts for the trailing week (live or cached)."""
    account_id = (proj.fairwind_account_id or "").strip()
    if not account_id:
        return "(none — no Fairwind account linked)"
    if not fw_client:
        return "(none — Fairwind unavailable)"
    t0 = time.perf_counter()
    try:
        docs, source = fetch_week_client_comms(
            fw_client,
            account_id,
            week_monday=date_from,
            report_friday=date_to,
            reuse=reuse,
        )
        coverage.setdefault("comms_by_project", {})[proj.canonical_name] = {
            "account_id": account_id,
            "docs": len(docs),
            "source": source,
            "seconds": round(time.perf_counter() - t0, 2),
        }
        return format_comms_excerpt(docs)
    except Exception as e:  # noqa: BLE001
        coverage.setdefault("comms_failures", []).append(
            {
                "project": proj.canonical_name,
                "error": str(e),
                "seconds": round(time.perf_counter() - t0, 2),
            }
        )
        return f"(none — Fairwind comms pull failed: {e})"


def _call_dates_for_project(
    proj: Project,
    *,
    session: Session,
    participant_emails: list[str],
    as_of: date,
    coverage: dict,
) -> dict:
    """Last/next client call from Transcript calendar-meetings API."""
    account_id = (proj.fairwind_account_id or "").strip()
    domains: list[str] = []
    if account_id:
        acct = (
            session.query(Account)
            .filter_by(fairwind_account_id=account_id)
            .one_or_none()
        )
        if acct:
            domains = list(acct.domains or [])
    if not domains:
        return empty_call_dates()

    t0 = time.perf_counter()
    try:
        fields, meta = call_dates_for_domains(
            domains, participant_emails, as_of=as_of,
            fairwind_account_id=account_id,
        )
        coverage.setdefault("meetings_by_project", {})[proj.canonical_name] = {
            **meta,
            "seconds": round(time.perf_counter() - t0, 2),
        }
        return fields
    except Exception as e:  # noqa: BLE001
        coverage.setdefault("meetings_failures", []).append(
            {
                "project": proj.canonical_name,
                "error": str(e),
                "seconds": round(time.perf_counter() - t0, 2),
            }
        )
        return empty_call_dates()


def _attach_card_links(card: dict, proj: Project) -> dict:
    """Add epic/project Jira browse URLs to a project card."""
    card.update(
        project_jira_links(
            jira_project_key=proj.jira_project_key,
            jira_scope=proj.jira_scope if isinstance(proj.jira_scope, dict) else None,
        )
    )
    return card


def _is_sample_run(pipeline: Pipeline, send_mode_override: str | None) -> bool:
    """SAMPLE banner only when delivery will not leave the system."""
    mode = (send_mode_override or pipeline.send_mode or SendMode.NONE.value).lower()
    if not pipeline.go_live:
        return True
    return mode in {SendMode.NONE.value, "none", ""}


def create_pending_run(session: Session, report_date: date | None = None) -> PipelineRun:
    """Snapshot run — report_date is the generation day (execute_run re-stamps it)."""
    pipeline = session.query(Pipeline).filter_by(key=PIPELINE_KEY).one()
    run = PipelineRun(
        pipeline_id=pipeline.id,
        report_date=report_date or date.today(),
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
    # Snapshot: the report always reflects the latest Jira/Fairwind state as of
    # the day it is generated. Client comms cover the trailing 7 days.
    as_of = date.today()
    comms_from = as_of - timedelta(days=6)
    run.report_date = as_of

    pipeline = session.get(Pipeline, run.pipeline_id)
    settings = get_settings()
    roster = list(session.query(Person).filter(Person.status != "out").all())
    roster_emails = design_roster_emails(roster)
    # Calendar API: design roster + Olga as participants on client meetings.
    call_participant_emails = design_participant_emails(
        list(roster_emails), settings=settings
    )
    health_projects = (
        session.query(Project)
        .filter_by(track_weekly_health=True, active=True)
        .order_by(Project.canonical_name)
        .all()
    )

    coverage: dict = {
        "as_of": as_of.isoformat(),
        "comms_from": comms_from.isoformat(),
        "projects": len(health_projects),
        "reuse_ingest": reuse_ingest,
    }
    all_jira_docs: list[Document] = []
    cards: list[dict] = []
    comms_by_project: dict[str, str] = {}

    try:
        jira_incomplete = False
        invoice_incomplete = False
        client = None
        fw_client = None
        if settings.jira_configured:
            client = JiraClient(settings)
        else:
            jira_incomplete = True
            coverage["jira_note"] = "JIRA_* not configured"
        if settings.fairwind_configured:
            try:
                fw_client = FairwindClient(settings)
            except FairwindError as e:
                invoice_incomplete = True
                coverage["fairwind_note"] = str(e)
        else:
            coverage["fairwind_note"] = "FW_* not configured"

        for proj in health_projects:
            key = (proj.jira_project_key or "").strip().upper()
            invoice_fields, live_meta = _commercial_for_project(
                proj,
                fw_client=fw_client,
                as_of=as_of,
                coverage=coverage,
                reuse=reuse_ingest,
            )
            # Subtitle/agreement facts come live from Fairwind. Signed hours: DB is
            # the source of truth (confirmed SOW hours); Fairwind only as fallback.
            agreement = dict(live_meta.get("agreement") or {})
            subtitle = (live_meta.get("subtitle") or "").strip()
            signed_h = (
                proj.signed_design_estimate_h
                if proj.signed_design_estimate_h is not None
                else live_meta.get("signed_design_estimate_h")
            )
            if invoice_fields.pop("_invoice_gap", False):
                invoice_incomplete = True
            comms_by_project[proj.canonical_name] = _comms_for_project(
                proj,
                fw_client=fw_client,
                date_from=comms_from,
                date_to=as_of,
                coverage=coverage,
                reuse=reuse_ingest,
            )
            call_fields = _call_dates_for_project(
                proj,
                session=session,
                participant_emails=call_participant_emails,
                as_of=as_of,
                coverage=coverage,
            )

            if not key or client is None:
                card = {
                    "display_name": proj.canonical_name,
                    "subtitle": subtitle,
                    "pending": True,
                    "rag": "a",
                    "rag_label": "Pending",
                    "highlights": [],
                    "client_action_count": 0,
                    "over_est_count": 0,
                    "_agreement": agreement,
                    "signed_estimate_display": fmt_hours(signed_h),
                    "signed_estimate_muted": signed_h is None,
                    "signed_estimate_sub": (
                        "signed design hours"
                        if signed_h is not None
                        else "no signed estimate yet"
                    ),
                    "logged_h": 0,
                    "logged_sub": "—",
                    **invoice_fields,
                    **call_fields,
                }
                cards.append(_attach_card_links(card, proj))
                continue

            docs = client.search_project_issues(key, event_date=as_of)
            all_jira_docs.extend(docs)
            tickets = [doc_to_ticket(d) for d in docs]
            tickets = apply_jira_scope(tickets, proj.jira_scope)
            # Prefer live epic title over Fairwind SOW name when scoped to an epic.
            epic_sub = epic_subtitle(tickets, proj.jira_scope)
            if epic_sub:
                subtitle = epic_sub
            tickets = [t for t in tickets if in_design_scope(t, roster_emails)]

            if not tickets and key:
                # Empty design scope → still show card with zeros, or pending for SGDCP-style
                card = build_project_burn(
                    display_name=proj.canonical_name,
                    subtitle=subtitle,
                    signed_estimate_h=signed_h,
                    agreement=agreement,
                    tickets=[],
                    as_of=as_of,
                )
                card["pending"] = True
                card["rag"] = "a"
                card["rag_label"] = "Pending"
                card["_agreement"] = agreement
                card.update(invoice_fields)
                card.update(call_fields)
                cards.append(_attach_card_links(card, proj))
                continue

            card = build_project_burn(
                display_name=proj.canonical_name,
                subtitle=subtitle,
                signed_estimate_h=signed_h,
                agreement=agreement,
                tickets=tickets,
                as_of=as_of,
            )
            card["_agreement"] = agreement
            card.update(invoice_fields)
            card.update(call_fields)
            cards.append(_attach_card_links(card, proj))

        cards, actions, synth = _synthesize(
            cards,
            as_of=as_of,
            comms_from=comms_from,
            comms_by_project=comms_by_project,
        )
        cards.sort(key=rag_sort_key)
        glance = glance_kpis(cards)
        glance["action_items"] = len(actions)
        digest = {
            "as_of": as_of.isoformat(),
            "comms_from": comms_from.isoformat(),
            "at_a_glance": glance,
            "projects": cards,
            "actions": actions,
        }
        coverage["jira_issues"] = len(all_jira_docs)
        coverage["jira_incomplete"] = jira_incomplete
        coverage["invoice_incomplete"] = invoice_incomplete
        coverage["incomplete"] = jira_incomplete or invoice_incomplete

        batch = IngestBatch(
            report_date=as_of,
            account_ids=[],
            started_at=_now(),
            finished_at=_now(),
            status="ok",
            doc_count=len(all_jira_docs),
            coverage={**coverage, "pipeline": PIPELINE_KEY},
        )
        session.add(batch)
        session.flush()
        run.ingest_batch_id = batch.id

        send_mode = send_mode_override or pipeline.send_mode
        html = render_weekly_health(
            digest,
            as_of,
            sample=_is_sample_run(pipeline, send_mode_override),
            coverage=coverage,
        )
    except Exception as e:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = _now()
        session.add(run)
        session.flush()
        return run

    run.status = RunStatus.FLAGGED if coverage.get("incomplete") else RunStatus.OK
    delivery = deliver(
        go_live=pipeline.go_live,
        send_mode=send_mode,
        html=html,
        recipients=pipeline.recipients,
        subject=email_subject_for_pipeline(PIPELINE_KEY, as_of),
        setup_owner_email=settings.setup_owner_email,
    )
    run.finished_at = _now()
    run.counts = {
        "projects": len(digest["projects"]),
        "jira_issues": coverage.get("jira_issues", 0),
        "coverage": coverage,
    }
    run.input_tokens = synth["input_tokens"]
    run.output_tokens = synth["output_tokens"]
    run.cost_usd = synth["cost_usd"]
    run.skill_version = synth.get("model") or synth["mode"]
    run.note = synth.get("note")
    session.add(run)
    session.flush()

    for doc in all_jira_docs:
        session.add(
            RunDocument(
                run_id=run.id,
                source=doc.source,
                external_id=doc.external_id,
                event_date=doc.event_date,
                person_id=None,
                project_id=None,
                included=True,
                exclusion_reason=None,
                title=doc.title,
            )
        )
    session.add(
        Artifact(
            run_id=run.id,
            kind="html",
            content=html,
            delivery_status=delivery.status,
            delivered_at=_now() if delivery.status in ("self", "draft", "sent") else None,
            message_id=delivery.message_id,
        )
    )
    session.add(
        Artifact(
            run_id=run.id,
            kind="json",
            content=json.dumps(digest, indent=2, default=str),
            delivery_status="none",
        )
    )
    if coverage.get("incomplete"):
        session.add(
            Flag(
                run_id=run.id,
                type=FlagType.INGEST_GAP,
                body="Weekly health inputs incomplete (Jira).",
            )
        )
    return run


def run_weekly_health(
    session: Session,
    *,
    reuse_ingest: bool = True,
    send_mode_override: str | None = None,
) -> PipelineRun:
    """Generate the snapshot report as of today."""
    run = create_pending_run(session)
    return execute_run(
        session, run, reuse_ingest=reuse_ingest, send_mode_override=send_mode_override
    )
