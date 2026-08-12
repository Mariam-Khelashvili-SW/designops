"""A2 Weekly Project Health & Budget — DB-backed orchestration.

Snapshot brief for Olga, dated the day it is generated: full-history Jira by
project key (design-scoped burn) + Fairwind client comms for the trailing
7 days. LLM writes health/highlights/actions only.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
    project_sort_key,
)
from designops.pipelines.weekly_health_figma import (
    attach_figma_to_cards,
    empty_figma_bundle,
    fetch_figma_comments_bundle,
    figma_excerpt_no_urls,
    figma_excerpt_not_configured,
    sum_figma_overdue_kpi,
)
from designops.pipelines.weekly_health_meetings import (
    call_dates_for_domains,
    design_participant_emails,
    empty_call_dates,
)

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "weekly-health.md"
PIPELINE_KEY = "weekly-health"
_INNER_PREFETCH_WORKERS = 4

_EMPTY_COMMERCIAL_META = {
    "subtitle": "",
    "agreement": {},
    "signed_design_estimate_h": None,
    "source": None,
}
_UNAVAILABLE_INVOICE = {
    "invoiced_label": "n/a",
    "invoiced_muted": True,
    "invoiced_sub": "Fairwind unavailable",
    "invoice_notes": None,
    "ux_invoice_lines": [],
    "ux_invoiced_total": 0.0,
    "_invoice_gap": True,
}
_NO_ACCOUNT_INVOICE = {
    "invoiced_label": "n/a",
    "invoiced_muted": True,
    "invoiced_sub": "no Fairwind account linked",
    "invoice_notes": None,
    "ux_invoice_lines": [],
    "ux_invoiced_total": 0.0,
    "_invoice_gap": False,
}


def _prefetch_concurrency(project_count: int, settings=None) -> int:
    """Fan-out workers — match Fairwind daily digest defaults."""
    s = settings or get_settings()
    cap = max(1, int(s.fw_export_concurrency))
    return max(1, min(cap, project_count or 1))


def _load_account_domains(session: Session, projects: list[Project]) -> dict[str, list[str]]:
    account_ids = {(p.fairwind_account_id or "").strip() for p in projects}
    account_ids.discard("")
    if not account_ids:
        return {}
    rows = (
        session.query(Account)
        .filter(Account.fairwind_account_id.in_(account_ids))
        .all()
    )
    return {a.fairwind_account_id: list(a.domains or []) for a in rows}


def _fetch_fairwind_pair(
    proj: SimpleNamespace,
    *,
    fw_client: FairwindClient | None,
    as_of: date,
    comms_from: date,
    reuse: bool,
) -> tuple[dict, list[dict]]:
    invoice_fields, live_meta, inv_snip = _fetch_commercial_bundle(
        proj, fw_client=fw_client, as_of=as_of, reuse=reuse
    )
    comms, comms_snip = _fetch_comms_bundle(
        proj,
        fw_client=fw_client,
        date_from=comms_from,
        date_to=as_of,
        reuse=reuse,
    )
    return (
        {
            "invoice_fields": invoice_fields,
            "live_meta": live_meta,
            "comms": comms,
        },
        [snip for snip in (inv_snip, comms_snip) if snip],
    )


def _fetch_jira_bundle(
    proj: Project,
    *,
    client: JiraClient | None,
    as_of: date,
    roster_emails: set[str],
) -> tuple[list[Document], list[dict], dict]:
    key = (proj.jira_project_key or "").strip().upper()
    if not key or client is None:
        return [], [], {}
    t0 = time.perf_counter()
    try:
        docs = client.search_project_issues(key, event_date=as_of)
        tickets = [doc_to_ticket(d) for d in docs]
        tickets = apply_jira_scope(tickets, proj.jira_scope)
        tickets = [t for t in tickets if in_design_scope(t, roster_emails)]
        return (
            docs,
            tickets,
            {
                "jira_by_project": {
                    proj.canonical_name: {
                        "key": key,
                        "issues": len(docs),
                        "scoped_design": len(tickets),
                        "seconds": round(time.perf_counter() - t0, 2),
                    }
                }
            },
        )
    except Exception as e:  # noqa: BLE001
        return [], [], {
            "jira_failures": [
                {
                    "project": proj.canonical_name,
                    "key": key,
                    "error": str(e),
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            ]
        }


def _fetch_call_dates_bundle(
    project_name: str,
    *,
    email_domains: list[str],
    fairwind_account_id: str | None,
    participant_emails: list[str],
    as_of: date,
    settings=None,
) -> tuple[dict, dict]:
    if not email_domains:
        return empty_call_dates(), {}
    t0 = time.perf_counter()
    try:
        fields, meta = call_dates_for_domains(
            email_domains,
            participant_emails,
            as_of=as_of,
            fairwind_account_id=fairwind_account_id,
            settings=settings,
        )
        return fields, {
            "meetings_by_project": {
                project_name: {
                    **meta,
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            }
        }
    except Exception as e:  # noqa: BLE001
        return empty_call_dates(), {
            "meetings_failures": [
                {
                    "project": project_name,
                    "error": str(e),
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            ]
        }


def _fetch_figma_bundle_for_project(
    proj: Project,
    *,
    comms_from: date,
    as_of: date,
    account_domains: list[str] | None = None,
    roster_emails: set[str] | None = None,
    settings=None,
) -> dict:
    from designops.adapters import figma as figma_api

    s = settings or get_settings()
    urls = list(proj.figma_urls or [])
    if not urls:
        return empty_figma_bundle(figma_excerpt_no_urls())
    if not figma_api.is_ready(s):
        return empty_figma_bundle(figma_excerpt_not_configured())
    return fetch_figma_comments_bundle(
        urls,
        since=comms_from,
        as_of=as_of,
        settings=s,
        account_domains=account_domains,
        roster_emails=roster_emails,
    )


def _prefetch_one_project(
    proj: Project,
    *,
    account_domains: dict[str, list[str]],
    as_of: date,
    comms_from: date,
    roster_emails: set[str],
    participant_emails: list[str],
    reuse: bool,
    settings=None,
) -> tuple[str, dict, list[dict]]:
    """Fetch Fairwind + Jira + calendar + Figma for one project (parallel inner fan-out)."""
    s = settings or get_settings()
    fw_client = None
    if s.fairwind_configured:
        try:
            fw_client = FairwindClient(s)
        except FairwindError:
            fw_client = None
    jira_client = None
    if s.jira_configured:
        try:
            jira_client = JiraClient(s)
        except RuntimeError:
            jira_client = None

    snap = _project_fairwind_snapshot(proj)
    account_id = (proj.fairwind_account_id or "").strip()
    domains = account_domains.get(account_id, [])
    snippets: list[dict] = []

    with ThreadPoolExecutor(max_workers=_INNER_PREFETCH_WORKERS) as inner:
        fw_fut = inner.submit(
            _fetch_fairwind_pair,
            snap,
            fw_client=fw_client,
            as_of=as_of,
            comms_from=comms_from,
            reuse=reuse,
        )
        jira_fut = inner.submit(
            _fetch_jira_bundle,
            proj,
            client=jira_client,
            as_of=as_of,
            roster_emails=roster_emails,
        )
        call_fut = inner.submit(
            _fetch_call_dates_bundle,
            proj.canonical_name,
            email_domains=domains,
            fairwind_account_id=account_id or None,
            participant_emails=participant_emails,
            as_of=as_of,
            settings=s,
        )
        figma_fut = inner.submit(
            _fetch_figma_bundle_for_project,
            proj,
            comms_from=comms_from,
            as_of=as_of,
            account_domains=domains,
            roster_emails=roster_emails,
            settings=s,
        )
        fw_data, fw_snips = fw_fut.result()
        jira_docs, tickets, jira_snip = jira_fut.result()
        call_fields, call_snip = call_fut.result()
        figma_bundle = figma_fut.result()

    snippets.extend(fw_snips)
    if jira_snip:
        snippets.append(jira_snip)
    if call_snip:
        snippets.append(call_snip)

    return (
        proj.canonical_name,
        {
            "fairwind": fw_data,
            "jira_docs": jira_docs,
            "tickets": tickets,
            "call_fields": call_fields,
            "figma": figma_bundle,
        },
        snippets,
    )


def _prefetch_all_project_inputs(
    projects: list[Project],
    *,
    account_domains: dict[str, list[str]],
    as_of: date,
    comms_from: date,
    roster_emails: set[str],
    participant_emails: list[str],
    reuse: bool,
    coverage: dict,
    settings=None,
    concurrency: int | None = None,
) -> dict[str, dict]:
    """Parallel per-project prefetch (Fairwind + Jira + meetings + Figma)."""
    s = settings or get_settings()
    results: dict[str, dict] = {}
    if not projects:
        return results

    workers = concurrency or _prefetch_concurrency(len(projects), s)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(
                _prefetch_one_project,
                proj,
                account_domains=account_domains,
                as_of=as_of,
                comms_from=comms_from,
                roster_emails=roster_emails,
                participant_emails=participant_emails,
                reuse=reuse,
                settings=s,
            )
            for proj in projects
        ]
        for fut in as_completed(futs):
            name, payload, snippets = fut.result()
            results[name] = payload
            for snip in snippets:
                _merge_coverage(coverage, snip)

    coverage["prefetch_seconds"] = round(time.perf_counter() - t0, 2)
    coverage["prefetch_concurrency"] = workers
    coverage["prefetch_inner_workers"] = _INNER_PREFETCH_WORKERS
    _merge_figma_prefetch_coverage(coverage, results, settings=s)
    coverage["fairwind_prefetch_seconds"] = coverage["prefetch_seconds"]
    coverage["fairwind_prefetch_concurrency"] = workers
    coverage["fairwind_prefetch_projects"] = len(projects)
    return results


def _merge_figma_prefetch_coverage(
    coverage: dict,
    prefetched: dict[str, dict],
    *,
    settings=None,
) -> None:
    from designops.adapters import figma as figma_api

    s = settings or get_settings()
    if figma_api.is_ready(s):
        coverage["figma_auth"] = figma_api.auth_mode(s)
    elif any(
        (payload.get("figma") or {}).get("excerpt") == figma_excerpt_not_configured()
        for payload in prefetched.values()
    ):
        coverage["figma_note"] = (
            "Figma not configured — Connect Figma or save PAT on Config"
        )

    totals = {
        "projects_with_urls": 0,
        "files": 0,
        "overdue_items": 0,
        "errors": [],
    }
    for name, payload in prefetched.items():
        bundle = payload.get("figma") or {}
        panel = bundle.get("panel") or {}
        counts = panel.get("counts") or {}
        if int(bundle.get("files") or 0) > 0:
            totals["projects_with_urls"] += 1
        totals["files"] += int(bundle.get("files") or 0)
        totals["overdue_items"] += int(counts.get("overdue_items") or 0)
        for err in bundle.get("errors") or []:
            totals["errors"].append({"project": name, "error": err})
    coverage["figma_prefetch"] = totals


def _build_card_from_prefetch(
    proj: Project,
    prefetched: dict,
    *,
    as_of: date,
    jira_available: bool,
) -> tuple[dict, list[Document], bool]:
    """Return (card, jira_docs, invoice_gap)."""
    fw = prefetched.get("fairwind") or {}
    invoice_fields = dict(fw.get("invoice_fields") or _UNAVAILABLE_INVOICE)
    live_meta = fw.get("live_meta") or dict(_EMPTY_COMMERCIAL_META)
    agreement = dict(live_meta.get("agreement") or {})
    subtitle = (live_meta.get("subtitle") or "").strip()
    signed_h = (
        proj.signed_design_estimate_h
        if proj.signed_design_estimate_h is not None
        else live_meta.get("signed_design_estimate_h")
    )
    invoice_gap = bool(invoice_fields.pop("_invoice_gap", False))
    call_fields = prefetched.get("call_fields") or empty_call_dates()
    tickets = list(prefetched.get("tickets") or [])
    jira_docs = list(prefetched.get("jira_docs") or [])
    key = (proj.jira_project_key or "").strip().upper()

    epic_sub = epic_subtitle(tickets, proj.jira_scope)
    if epic_sub:
        subtitle = epic_sub

    if not key or not jira_available:
        card = {
            "display_name": proj.canonical_name,
            "subtitle": subtitle,
            "pending": True,
            "highlights": [],
            "client_action_count": 0,
            "over_est_count": 0,
            "_agreement": agreement,
            "signed_estimate_display": fmt_hours(signed_h),
            "signed_estimate_muted": signed_h is None,
            "signed_estimate_sub": (
                "signed design hours" if signed_h is not None else "no signed estimate yet"
            ),
            "logged_h": 0,
            "logged_sub": "—",
            **invoice_fields,
            **call_fields,
        }
        return _attach_card_links(card, proj), jira_docs, invoice_gap

    if not tickets:
        card = build_project_burn(
            display_name=proj.canonical_name,
            subtitle=subtitle,
            signed_estimate_h=signed_h,
            agreement=agreement,
            tickets=[],
            as_of=as_of,
        )
        card["pending"] = True
        card["_agreement"] = agreement
        card.update(invoice_fields)
        card.update(call_fields)
        return _attach_card_links(card, proj), jira_docs, invoice_gap

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
    return _attach_card_links(card, proj), jira_docs, invoice_gap


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
    figma_by_project: dict[str, dict] | None = None,
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
        f"FIGMA_COMMENTS is a precomputed summary per project: counts of new, "
        f"resolved, still-open and overdue (>1 week) items, plus verbatim quotes "
        f"with pin links for items open on our side since {comms_from.isoformat()}.",
        "Fill verdict, highlights, and roll-up actions. Numbers are already set.",
        "Surface Figma-sourced action items with evidence tagged "
        "'· {Project} · Figma comments'. Do not restate Figma counts in verdict "
        "or highlights — they appear in the Figma panel.",
        "",
    ]
    for c in cards:
        if c.get("pending"):
            continue
        chunks.append(f"### {c['display_name']}")
        chunks.append(
            f"Signed={c.get('signed_estimate_display')}; logged={c.get('logged_h')}h; "
            f"over_est={c.get('over_est_count')}; "
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
        chunks.append("FIGMA_COMMENTS:")
        bundle = (figma_by_project or {}).get(c["display_name"]) or {}
        chunks.append(bundle.get("excerpt") or "(none — no Figma files linked on Weekly health)")
        chunks.append("")
    user = "\n".join(chunks)

    # Code-side highlights fallback when LLM unavailable
    for c in cards:
        if c.get("pending"):
            continue
        c.setdefault("highlights", [])

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
            "note": "No ANTHROPIC_API_KEY — code-side ageing actions.",
        }

    result = LLMClient(settings).synthesize(system=system, user_content=user, max_tokens=7000)
    try:
        parsed = parse_digest_json(result.text)
    except Exception as e:  # noqa: BLE001 — keep code-side health rather than fail the run
        actions = _fallback_actions(cards)
        for c in cards:
            c.pop("_agreement", None)
        return cards, actions, {
            "mode": "fallback",
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": float(result.cost_usd),
            "note": f"LLM synthesis failed ({type(e).__name__}: {e}); used code-side actions.",
        }
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


def _project_fairwind_snapshot(proj: Project | SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        canonical_name=proj.canonical_name,
        fairwind_account_id=(proj.fairwind_account_id or "").strip(),
        jira_project_key=proj.jira_project_key,
    )


def _fetch_commercial_bundle(
    proj: SimpleNamespace,
    *,
    fw_client: FairwindClient | None,
    as_of: date,
    reuse: bool,
) -> tuple[dict, dict, dict]:
    """Return (invoice_fields, live_meta, coverage_snippet). Thread-safe (no shared mutables)."""
    empty_meta = dict(_EMPTY_COMMERCIAL_META)
    account_id = (proj.fairwind_account_id or "").strip()
    if not account_id:
        return dict(_NO_ACCOUNT_INVOICE), empty_meta, {}
    if not fw_client:
        return dict(_UNAVAILABLE_INVOICE), empty_meta, {}
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
        snippet = {
            "invoices_by_project": {
                proj.canonical_name: {
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
            }
        }
        summary["_invoice_gap"] = False
        return summary, meta, snippet
    except Exception as e:  # noqa: BLE001 — one account gap, not a failed run
        return (
            {
                "invoiced_label": "n/a",
                "invoiced_muted": True,
                "invoiced_sub": "Fairwind invoice pull failed",
                "invoice_notes": None,
                "ux_invoice_lines": [],
                "ux_invoiced_total": 0.0,
                "_invoice_gap": True,
            },
            empty_meta,
            {
                "invoice_failures": [
                    {
                        "project": proj.canonical_name,
                        "error": str(e),
                        "seconds": round(time.perf_counter() - t0, 2),
                    }
                ]
            },
        )


def _fetch_comms_bundle(
    proj: SimpleNamespace,
    *,
    fw_client: FairwindClient | None,
    date_from: date,
    date_to: date,
    reuse: bool,
) -> tuple[str, dict]:
    """Return (comms_excerpt, coverage_snippet). Thread-safe."""
    account_id = (proj.fairwind_account_id or "").strip()
    if not account_id:
        return "(none — no Fairwind account linked)", {}
    if not fw_client:
        return "(none — Fairwind unavailable)", {}
    t0 = time.perf_counter()
    try:
        docs, source = fetch_week_client_comms(
            fw_client,
            account_id,
            week_monday=date_from,
            report_friday=date_to,
            reuse=reuse,
        )
        return format_comms_excerpt(docs), {
            "comms_by_project": {
                proj.canonical_name: {
                    "account_id": account_id,
                    "docs": len(docs),
                    "source": source,
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            }
        }
    except Exception as e:  # noqa: BLE001
        return f"(none — Fairwind comms pull failed: {e})", {
            "comms_failures": [
                {
                    "project": proj.canonical_name,
                    "error": str(e),
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            ]
        }


def _merge_coverage(coverage: dict, snippet: dict) -> None:
    for key, value in snippet.items():
        if isinstance(value, dict):
            coverage.setdefault(key, {}).update(value)
        elif isinstance(value, list):
            coverage.setdefault(key, []).extend(value)
        else:
            coverage[key] = value


def _prefetch_fairwind_for_projects(
    projects: list,
    *,
    as_of: date,
    comms_from: date,
    coverage: dict,
    reuse: bool,
    settings=None,
    concurrency: int | None = None,
) -> dict[str, dict]:
    """Fan out commercial + week-comms per account (bounded concurrency).

    Each worker uses its own FairwindClient (httpx is not thread-safe on one instance).
    Returns {canonical_name: {invoice_fields, live_meta, comms}}.
    """
    s = settings or get_settings()
    snapshots = [_project_fairwind_snapshot(p) for p in projects]
    results: dict[str, dict] = {}
    if not snapshots:
        return results

    def _one(proj: SimpleNamespace) -> tuple[str, dict, list[dict]]:
        fw_client = None
        if s.fairwind_configured:
            try:
                fw_client = FairwindClient(s)
            except FairwindError:
                fw_client = None
        fw_data, snippets = _fetch_fairwind_pair(
            proj,
            fw_client=fw_client,
            as_of=as_of,
            comms_from=comms_from,
            reuse=reuse,
        )
        return proj.canonical_name, fw_data, snippets

    workers = concurrency or _prefetch_concurrency(len(snapshots), s)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, snap) for snap in snapshots]
        for fut in as_completed(futs):
            name, payload, snippets = fut.result()
            results[name] = payload
            for snip in snippets:
                _merge_coverage(coverage, snip)
    coverage["fairwind_prefetch_seconds"] = round(time.perf_counter() - t0, 2)
    coverage["fairwind_prefetch_concurrency"] = workers
    coverage["fairwind_prefetch_projects"] = len(snapshots)
    return results


def prewarm_fairwind_caches(
    *,
    reuse: bool = True,
    concurrency: int | None = None,
) -> dict:
    """Warm same-day commercial + week-comms disk caches (no run, no email).

    Intended for the scheduler ~30 minutes before the weekly-health send so the
    scheduled generate hits fairwind-cached instead of cold-polling Fairwind.
    """
    from designops.core.db import session_scope

    as_of = date.today()
    comms_from = as_of - timedelta(days=6)
    settings = get_settings()
    coverage: dict = {
        "as_of": as_of.isoformat(),
        "comms_from": comms_from.isoformat(),
        "prewarm": True,
        "reuse_ingest": reuse,
    }
    print(
        f"  ▶ weekly-health Fairwind pre-warm as_of={as_of} "
        f"comms_from={comms_from} reuse={reuse}",
        flush=True,
    )
    if not settings.fairwind_configured:
        coverage["ok"] = False
        coverage["fairwind_note"] = "FW_* not configured"
        print("  ⚠ weekly-health pre-warm skipped — Fairwind not configured", flush=True)
        return coverage

    with session_scope() as s:
        projects = (
            s.query(Project)
            .filter_by(track_weekly_health=True, active=True)
            .order_by(Project.canonical_name)
            .all()
        )
        snapshots = [_project_fairwind_snapshot(p) for p in projects]

    _prefetch_fairwind_for_projects(
        snapshots,
        as_of=as_of,
        comms_from=comms_from,
        coverage=coverage,
        reuse=reuse,
        settings=settings,
        concurrency=concurrency,
    )
    sources = [
        (coverage.get("invoices_by_project") or {}).get(name, {}).get("source")
        for name in (p.canonical_name for p in snapshots)
    ]
    coverage["ok"] = True
    coverage["commercial_cached"] = sum(1 for s in sources if s == "fairwind-cached")
    coverage["commercial_live"] = sum(1 for s in sources if s == "fairwind")
    print(
        f"  ✓ weekly-health pre-warm done projects={len(snapshots)} "
        f"live={coverage['commercial_live']} cached={coverage['commercial_cached']} "
        f"seconds={coverage.get('fairwind_prefetch_seconds')}",
        flush=True,
    )
    return coverage


def _commercial_for_project(
    proj: Project,
    *,
    fw_client: FairwindClient | None,
    as_of: date,
    coverage: dict,
    reuse: bool = True,
) -> tuple[dict, dict]:
    """Fairwind invoices + agreement/opportunity meta (live or cached)."""
    invoice_fields, meta, snippet = _fetch_commercial_bundle(
        _project_fairwind_snapshot(proj),
        fw_client=fw_client,
        as_of=as_of,
        reuse=reuse,
    )
    _merge_coverage(coverage, snippet)
    return invoice_fields, meta


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
    text, snippet = _fetch_comms_bundle(
        _project_fairwind_snapshot(proj),
        fw_client=fw_client,
        date_from=date_from,
        date_to=date_to,
        reuse=reuse,
    )
    _merge_coverage(coverage, snippet)
    return text


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
    figma_by_project: dict[str, dict] = {}

    try:
        jira_incomplete = not settings.jira_configured
        invoice_incomplete = False
        if jira_incomplete:
            coverage["jira_note"] = "JIRA_* not configured"
        if not settings.fairwind_configured:
            coverage["fairwind_note"] = "FW_* not configured"

        account_domains = _load_account_domains(session, health_projects)
        prefetched = _prefetch_all_project_inputs(
            health_projects,
            account_domains=account_domains,
            as_of=as_of,
            comms_from=comms_from,
            roster_emails=roster_emails,
            participant_emails=call_participant_emails,
            reuse=reuse_ingest,
            coverage=coverage,
            settings=settings,
        )

        figma_by_project = {
            name: (payload.get("figma") or {}) for name, payload in prefetched.items()
        }
        comms_by_project = {
            name: (payload.get("fairwind") or {}).get("comms") or "(none)"
            for name, payload in prefetched.items()
        }

        if coverage.get("jira_failures"):
            jira_incomplete = True
        if coverage.get("invoice_failures"):
            invoice_incomplete = True

        for proj in health_projects:
            payload = prefetched.get(proj.canonical_name) or {}
            card, docs, invoice_gap = _build_card_from_prefetch(
                proj,
                payload,
                as_of=as_of,
                jira_available=settings.jira_configured,
            )
            if invoice_gap:
                invoice_incomplete = True
            all_jira_docs.extend(docs)
            cards.append(card)

        attach_figma_to_cards(cards, figma_by_project)
        cards, actions, synth = _synthesize(
            cards,
            as_of=as_of,
            comms_from=comms_from,
            comms_by_project=comms_by_project,
            figma_by_project=figma_by_project,
        )
        cards.sort(key=project_sort_key)
        figma_overdue = sum_figma_overdue_kpi(figma_by_project)
        glance = glance_kpis(cards, figma_overdue=figma_overdue)
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
