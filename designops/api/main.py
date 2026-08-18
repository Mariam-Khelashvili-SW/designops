"""FastAPI read UI (§0, §12.3). Server-rendered Jinja, no SPA, no build step.

Screens: Pipelines · Run log · Artifact viewer (3 panes) · Accounts · Config.
On-demand run: POST /pipelines/daily-digest/run (§12.1). Manual Generate never
emails (send_mode forced to none); schedule Delivery + go_live gate still apply
to cron runs (§12.4).
"""

from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from designops.adapters import figma_oauth, google_oauth
from designops.adapters.delivery import send_digest
from designops.adapters.fairwind import FairwindClient, FairwindError
from designops.core.config import get_settings
from designops.core.identity import effective_status
from designops.core.db import get_db
from designops.core.enums import PersonStatus, SendMode
from designops.core.models import (
    Account,
    Artifact,
    CallSummaryDraft,
    Flag,
    IntakeDraft,
    Person,
    Pipeline,
    PipelineRun,
    Project,
    RunDocument,
)
from designops.pipelines.daily_digest import create_pending_run
from designops.pipelines.email_subjects import email_subject_for_pipeline
from designops.pipelines.weekly_availability import resolve_week_monday
from designops.pipelines.weekly_backlog import (
    PIPELINE_KEY as WEEKLY_KEY,
    create_pending_run as create_weekly_pending_run,
)
from designops.api.auth import (
    credentials_ok,
    is_authenticated,
    is_public_path,
    login_enabled,
    mark_logged_in,
    mark_logged_out,
    safe_next_url,
    session_secret,
)
from designops.api.run_status import explain_run_status, status_why
from designops.pipelines.weekly_health import (
    PIPELINE_KEY as WEEKLY_HEALTH_KEY,
    create_pending_run as create_weekly_health_pending_run,
)

# The on-demand explorer pulls the full set the user asked for: internal + external
# emails, transcripts, and Jira (distinct from the daily job's jira+transcripts).
EXPLORER_DATA_TYPES = ["emails_internal", "emails_external", "jira", "transcripts"]

app = FastAPI(title="Design Ops — A1 Daily Ops Digest")
_TEMPLATES = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))
templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else "—"
templates.env.globals["login_enabled"] = login_enabled
templates.env.globals["status_why"] = status_why
templates.env.globals["status_reasons"] = explain_run_status


def _fmt_when(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.strftime("%a %-d %b, %H:%M").replace(" 0", " ")


def _latest_run(db: Session, key: str) -> PipelineRun | None:
    pipeline = db.query(Pipeline).filter_by(key=key).one_or_none()
    if not pipeline:
        return None
    return (
        db.query(PipelineRun)
        .filter_by(pipeline_id=pipeline.id)
        .order_by(PipelineRun.started_at.desc())
        .first()
    )


def _shell_context() -> dict:
    """Sidebar badges + next-send footer. Own session so any page can call it."""
    empty = {"drafts": 0, "people": 0, "next_send": None, "next_label": None}
    try:
        from designops.api.scheduler import next_run_time
        from designops.core.db import session_scope

        with session_scope() as db:
            drafts = db.query(CallSummaryDraft).count()
            people_n = (
                db.query(Person).filter(Person.status != PersonStatus.OUT.value).count()
            )
            soonest: datetime | None = None
            label: str | None = None
            for key, name in (
                ("daily-digest", "Daily Pulse"),
                ("weekly-backlog", "Weekly planning"),
                ("weekly-health", "Project health"),
            ):
                nrt = next_run_time(key)
                if nrt is not None and (soonest is None or nrt < soonest):
                    soonest = nrt
                    label = name
            return {
                "drafts": drafts,
                "people": people_n,
                "next_send": _fmt_when(soonest),
                "next_label": label,
            }
    except Exception:  # noqa: BLE001 — never break a page for a badge
        return empty


templates.env.globals["shell"] = _shell_context


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not login_enabled() or is_public_path(request.url.path):
        return await call_next(request)
    if is_authenticated(request):
        return await call_next(request)
    if request.method in {"GET", "HEAD"}:
        nxt = quote(
            request.url.path
            + (("?" + request.url.query) if request.url.query else "")
        )
        return RedirectResponse(f"/login?next={nxt}", status_code=302)
    return JSONResponse({"error": "login required"}, status_code=401)


app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    session_cookie="designops_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)


@app.on_event("startup")
def _ensure_pipelines() -> None:
    """Fresh managed DBs get schema from alembic but no pipeline rows — insert defaults."""
    from designops.core.bootstrap import ensure_pipelines
    from designops.core.db import session_scope

    with session_scope() as s:
        ensure_pipelines(s)


@app.on_event("startup")
def _reap_orphaned_runs() -> None:
    """Workers are in-process threads, so any run still 'running' at boot was orphaned by
    the previous process's exit. Mark them failed so they don't hang forever."""
    from designops.core.db import session_scope

    with session_scope() as s:
        for r in s.query(PipelineRun).filter_by(status="running").all():
            r.status = "failed"
            r.error = "Interrupted by a server restart (worker did not finish)."
            r.finished_at = datetime.now()
            s.add(r)


@app.on_event("startup")
def _start_scheduler() -> None:
    from designops.api.scheduler import start_scheduler

    start_scheduler()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    nxt = safe_next_url(next)
    if not login_enabled() or is_authenticated(request):
        return RedirectResponse(nxt, status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "next": nxt,
            "username": "admin",
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    nxt = safe_next_url(next)
    if credentials_ok(username, password):
        mark_logged_in(request)
        return RedirectResponse(nxt, status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Wrong username or password.",
            "next": nxt,
            "username": username.strip(),
        },
        status_code=401,
    )


@app.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    mark_logged_out(request)
    return RedirectResponse("/login" if login_enabled() else "/", status_code=302)


_OVERVIEW_ICOS = {
    "daily": '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2h10v12H3zM5.5 5.5h5M5.5 8h5M5.5 10.5h3"/></svg>',
    "plan": '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11v9.5h-11zM2.5 6.8h11M5.5 2v2.6M10.5 2v2.6"/></svg>',
    "health": '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="M2 9h3l1.5-4 2 8 2-5 1.2 1H14"/></svg>',
}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    from designops.api.scheduler import next_run_time

    today = date.today()
    people = db.query(Person).all()
    active_n = 0
    leave_n = 0
    for p in people:
        st = effective_status(p.status, p.leave_until, today, leave_from=p.leave_from)
        if st == PersonStatus.ACTIVE.value:
            active_n += 1
        elif st == PersonStatus.ON_LEAVE.value:
            leave_n += 1
    tracked_n = (
        db.query(Project)
        .filter_by(track_weekly_health=True, active=True)
        .count()
    )

    def card(key: str, *, title: str, sub: str, ico: str, open_url: str,
             run_url: str, run_fields: list[tuple[str, str]], next_key: str) -> dict:
        run = _latest_run(db, key)
        nrt = next_run_time(next_key)
        last_for = None
        if run and run.report_date:
            last_for = run.report_date.strftime("%a %-d %b").replace(" 0", " ")
        return {
            "title": title,
            "sub": sub,
            "ico": ico,
            "last_for": last_for,
            "last_at": _fmt_when(run.started_at) if run else None,
            "status": run.status if run else None,
            "status_why": status_why(run) if run else "",
            "next": _fmt_when(nrt),
            "open_url": open_url,
            "run_url": run_url,
            "run_fields": run_fields,
        }

    default_date = _prev_working_day(today).isoformat()
    default_week = resolve_week_monday(today).isoformat()
    cards = [
        card(
            "daily-digest",
            title="Daily Pulse",
            sub="Every weekday · yesterday's work",
            ico=_OVERVIEW_ICOS["daily"],
            open_url="/daily-report",
            run_url="/daily-report/run",
            run_fields=[("report_date", default_date), ("reuse_ingest", "on")],
            next_key="daily-digest",
        ),
        card(
            "weekly-backlog",
            title="Weekly planning board",
            sub="Mondays · the week ahead",
            ico=_OVERVIEW_ICOS["plan"],
            open_url="/weekly-backlog",
            run_url="/weekly-backlog/run",
            run_fields=[("week_of", default_week), ("reuse_ingest", "on")],
            next_key="weekly-backlog",
        ),
        card(
            "weekly-health",
            title="Project health & budget",
            sub="Weekly · burn vs signed",
            ico=_OVERVIEW_ICOS["health"],
            open_url="/weekly-health",
            run_url="/weekly-health/run",
            run_fields=[("reuse_ingest", "on")],
            next_key="weekly-health",
        ),
    ]
    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "nav": "overview",
            "cards": cards,
            "active_n": active_n,
            "leave_n": leave_n,
            "tracked_n": tracked_n,
        },
    )


# --- Daily report -------------------------------------------------------------
def _prev_working_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _cron_to_fields(cron: str | None) -> tuple[str, str]:
    """cron 'M H * * D' -> ('HH:MM', 'weekdays'|'all') for the schedule form."""
    try:
        m, h, _, _, dow = (cron or "0 12 * * mon-fri").split()[:5]
        return f"{int(h):02d}:{int(m):02d}", ("all" if dow == "*" else "weekdays")
    except (ValueError, TypeError):
        return "12:00", "weekdays"


# APScheduler day-of-week names (0=Mon … 6=Sun when numeric).
_CRON_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")
_CRON_DOW_ALIASES = {
    "0": "mon",
    "1": "tue",
    "2": "wed",
    "3": "thu",
    "4": "fri",
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    **{d: d for d in _CRON_WEEKDAYS},
}


def _cron_weekday(cron: str | None, *, default: str = "mon") -> str:
    """Extract a single weekday token from cron (defaults to Monday)."""
    try:
        raw = (cron or "").split()[4].strip().lower()
    except (IndexError, AttributeError):
        return default
    return _CRON_DOW_ALIASES.get(raw, default)


def _pipeline_schedule_delivery(p: Pipeline) -> str:
    """What happens when the cron job fires."""
    mode = (p.send_mode or SendMode.NONE.value).lower()
    if mode == SendMode.NONE.value:
        return "Generate only — no email"
    if mode == SendMode.SEND.value:
        if p.go_live:
            rec = ", ".join(p.recipients) if p.recipients else "(no recipients set)"
            return f"Generate + email to {rec}"
        return "Generate only — go_live is off (email blocked)"
    if mode == SendMode.SELF.value:
        return "Generate + email to self (setup owner)"
    if mode == SendMode.DRAFT.value:
        return "Generate + save Gmail draft"
    return f"Generate ({mode})"


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report(request: Request, db: Session = Depends(get_db)):
    enabled = (
        db.query(Account).filter_by(digest_enabled=True).order_by(Account.name).all()
    )
    team = (
        db.query(Person)
        .filter(Person.status != "out")
        .order_by(Person.status, Person.full_name)
        .all()
    )
    pipeline = db.query(Pipeline).filter_by(key="daily-digest").one_or_none()
    recent = (
        db.query(PipelineRun)
        .filter_by(pipeline_id=pipeline.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(20)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    # schedule config from the pipeline row (cron -> HH:MM + weekdays/every-day)
    sched_time, sched_days = _cron_to_fields(pipeline.schedule_cron if pipeline else None)
    from designops.api.scheduler import next_run_time, scheduled_report_date

    nrt = next_run_time()
    report_day = scheduled_report_date("daily-digest", nrt) if nrt else None
    return templates.TemplateResponse(
        "daily_report.html",
        {
            "request": request, "nav": "daily", "enabled": enabled, "team": team,
            "recent": recent, "default_date": _prev_working_day(date.today()).isoformat(),
            "anthropic_ready": s.anthropic_configured,
            "data_types": s.fw_data_types,
            # schedule & delivery
            "sched_enabled": bool(pipeline and pipeline.enabled),
            "sched_time": sched_time, "sched_days": sched_days,
            "sched_recipients": ", ".join(pipeline.recipients) if pipeline else "",
            "sched_send": (pipeline.send_mode if pipeline else "none"),
            "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
            "sched_covers": (
                report_day.strftime("%a %-d %b").replace(" 0", " ") if report_day else None
            ),
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
        },
    )


CALL_SUMMARY_PAGE_SIZE = 50

# In-flight call-summary generations: transcript_id → started_at (epoch seconds)
_call_summary_pending: dict[str, float] = {}
_call_summary_failed: dict[str, str] = {}
# LLM extract+critic+compose can be slow; past this, treat as dead so UI unblocks.
_CALL_SUMMARY_PENDING_TIMEOUT_SEC = 15 * 60


def _expire_stale_call_summary_jobs() -> None:
    """Mark hung background jobs failed so pending UI and the generate lock clear."""
    import time as _time

    now = _time.time()
    for tid, started in list(_call_summary_pending.items()):
        if now - float(started) < _CALL_SUMMARY_PENDING_TIMEOUT_SEC:
            continue
        _call_summary_pending.pop(tid, None)
        _call_summary_failed.setdefault(
            tid,
            "Draft generation timed out after 15 minutes — try Regenerate.",
        )


@app.get("/call-summary", response_class=HTMLResponse)
def call_summary_page(request: Request, db: Session = Depends(get_db)):
    from designops.pipelines.call_summary import (
        load_designer_config,
        resolve_designer_emails,
        list_matching_calls,
        prune_older_drafts,
    )

    s = get_settings()
    # Keep one draft per meeting (cleanup any duplicates from earlier multi-clicks)
    prune_older_drafts(db)
    tab = (request.query_params.get("tab") or "calls").strip().lower()
    if tab not in ("designers", "calls", "drafts"):
        tab = "calls"

    # In-memory pending is the source of truth (survives refresh / tab switches).
    # URL ?pending= is optional; restore from memory when missing.
    url_pending = (request.query_params.get("pending") or "").strip()
    try:
        url_pending_since = float(request.query_params.get("pending_since") or "0")
    except ValueError:
        url_pending_since = 0.0

    _expire_stale_call_summary_jobs()

    # Resolve finished / failed jobs from memory (any tab). Pending stays until
    # this handler sees the draft — the worker must not clear it on success.
    completed_redirect: RedirectResponse | None = None
    for tid, started in list(_call_summary_pending.items()):
        err = _call_summary_failed.pop(tid, None)
        if err:
            _call_summary_pending.pop(tid, None)
            if tid == url_pending or not completed_redirect:
                completed_redirect = RedirectResponse(
                    f"/call-summary?tab={tab}&flash={quote(err)}&flash_type=error",
                    status_code=303,
                )
            continue
        latest = (
            db.query(CallSummaryDraft)
            .filter(CallSummaryDraft.transcript_id == tid)
            .order_by(CallSummaryDraft.generated_at.desc())
            .first()
        )
        if latest is None:
            continue
        gen_ts = latest.generated_at.timestamp() if latest.generated_at else 0.0
        if gen_ts >= started - 2:
            _call_summary_pending.pop(tid, None)
            completed_redirect = RedirectResponse(
                f"/call-summary?tab=drafts&draft_id={latest.id}&flash=Draft+generated",
                status_code=303,
            )
    if url_pending and url_pending in _call_summary_failed:
        err = _call_summary_failed.pop(url_pending)
        return RedirectResponse(
            f"/call-summary?tab={tab}&flash={quote(err)}&flash_type=error",
            status_code=303,
        )
    if completed_redirect is not None:
        return completed_redirect

    pending_ids = set(_call_summary_pending.keys())
    pending_busy = bool(pending_ids)
    if pending_ids:
        pending_tid, pending_since = max(
            ((t, _call_summary_pending[t]) for t in pending_ids),
            key=lambda kv: kv[1],
        )
    else:
        pending_tid = None
        pending_since = None
        # Stale URL after process restart with no in-memory job
        if url_pending and url_pending_since > 0:
            latest = (
                db.query(CallSummaryDraft)
                .filter(CallSummaryDraft.transcript_id == url_pending)
                .order_by(CallSummaryDraft.generated_at.desc())
                .first()
            )
            if latest and latest.generated_at and latest.generated_at.timestamp() >= url_pending_since - 2:
                return RedirectResponse(
                    f"/call-summary?tab=drafts&draft_id={latest.id}&flash=Draft+generated",
                    status_code=303,
                )

    cfg = load_designer_config(db)
    designers = resolve_designer_emails(db)
    people = (
        db.query(Person)
        .filter(Person.status != "out")
        .order_by(Person.full_name)
        .all()
    )
    drafts = (
        db.query(CallSummaryDraft)
        .order_by(CallSummaryDraft.generated_at.desc())
        .limit(50)
        .all()
    )

    draft_id = (request.query_params.get("draft_id") or "").strip()
    viewed = None
    if draft_id:
        try:
            viewed = db.get(CallSummaryDraft, draft_id)
        except Exception:  # noqa: BLE001 — invalid UUID
            viewed = None
    if tab == "drafts" and not viewed and drafts:
        viewed = drafts[0]
        draft_id = str(viewed.id)

    q = (request.query_params.get("q") or "").strip()
    filter_account = (request.query_params.get("account") or "").strip()
    filter_designer = (request.query_params.get("designer") or "").strip()
    filter_date_from = (request.query_params.get("date_from") or "").strip()
    filter_date_to = (request.query_params.get("date_to") or "").strip()
    filter_draft = (request.query_params.get("draft") or "").strip().lower()
    if filter_draft not in ("", "yes", "no"):
        filter_draft = ""
    page = max(1, int(request.query_params.get("page") or "1") or 1)
    calls: list = []
    calls_total = 0
    call_facets: dict = {"accounts": [], "designers": []}
    if tab == "calls" and designers:
        try:
            calls, calls_total, call_facets = list_matching_calls(
                db,
                search=q,
                account=filter_account,
                designer=filter_designer,
                date_from=filter_date_from,
                date_to=filter_date_to,
                draft=filter_draft,
                limit=CALL_SUMMARY_PAGE_SIZE,
                offset=(page - 1) * CALL_SUMMARY_PAGE_SIZE,
            )
        except Exception as e:  # noqa: BLE001
            return templates.TemplateResponse(
                "call_summary.html",
                {
                    "request": request,
                    "nav": "call_summary",
                    "tab": tab,
                    "flash": f"Failed to load calls: {e}",
                    "flash_type": "error",
                    "pending_tid": pending_tid or None,
                    "pending_since": _call_summary_pending.get(pending_tid) if pending_tid else None,
                    "pending_ids": list(_call_summary_pending.keys()),
                    "pending_busy": bool(_call_summary_pending),
                    "anthropic_ready": s.anthropic_configured,
                    "transcript_api_ready": s.transcript_api_configured,
                    "people": people,
                    "include_roles_csv": ", ".join(cfg.get("include_roles") or []),
                    "manual_emails_text": "\n".join(cfg.get("manual_emails") or []),
                    "selected_person_ids": set(cfg.get("selected_person_ids") or []),
                    "designer_count": len(designers),
                    "calls": [],
                    "calls_total": 0,
                    "call_facets": call_facets,
                    "q": q,
                    "filter_account": filter_account,
                    "filter_designer": filter_designer,
                    "filter_date_from": filter_date_from,
                    "filter_date_to": filter_date_to,
                    "filter_draft": filter_draft,
                    "calls_qs": "tab=calls",
                    "page": page,
                    "page_size": CALL_SUMMARY_PAGE_SIZE,
                    "total_pages": 1,
                    "drafts": drafts,
                    "drafts_total": len(drafts),
                    "viewed": viewed,
                    "draft_id": draft_id,
                    "extraction_json_pretty": "",
                    "fact_sheet_pretty": "",
                    "review_table": [],
                    "separate_email": None,
                    "draft_status": None,
                    "body_html": "",
                    "body_plain": "",
                    "kept_body_html": "",
                    "kept_body_plain": "",
                    "kept_body_text": None,
                },
            )

    total_pages = max(1, (calls_total + CALL_SUMMARY_PAGE_SIZE - 1) // CALL_SUMMARY_PAGE_SIZE)
    from urllib.parse import urlencode as _urlencode

    calls_filter_params: dict[str, str] = {"tab": "calls"}
    if q:
        calls_filter_params["q"] = q
    if filter_account:
        calls_filter_params["account"] = filter_account
    if filter_designer:
        calls_filter_params["designer"] = filter_designer
    if filter_date_from:
        calls_filter_params["date_from"] = filter_date_from
    if filter_date_to:
        calls_filter_params["date_to"] = filter_date_to
    if filter_draft:
        calls_filter_params["draft"] = filter_draft
    calls_qs = _urlencode(calls_filter_params)
    flash = request.query_params.get("flash")
    flash_type = request.query_params.get("flash_type") or "ok"
    if pending_ids and not flash:
        flash = "Generating draft… you can switch tabs; status is kept until it finishes."
        flash_type = "ok"

    import json as _json

    extraction_json_pretty = ""
    body_html = ""
    body_plain = ""
    kept_body_html = ""
    kept_body_plain = ""
    review_table: list = []
    separate_email = None
    fact_sheet_pretty = ""
    draft_status = None
    kept_body_text = None
    facts_only: dict = {}
    if viewed and viewed.extraction_json is not None:
        raw_ext = viewed.extraction_json if isinstance(viewed.extraction_json, dict) else {}
        pipeline_meta = raw_ext.get("_pipeline") if isinstance(raw_ext.get("_pipeline"), dict) else {}
        review_table = list(pipeline_meta.get("review_table") or [])
        separate_email = pipeline_meta.get("separate_email_recommended")
        kept_body_text = pipeline_meta.get("kept_body")
        if isinstance(kept_body_text, str) and not kept_body_text.strip():
            kept_body_text = None
        facts_only = {k: v for k, v in raw_ext.items() if k != "_pipeline"}
        fact_sheet_pretty = _json.dumps(facts_only, indent=2, default=str)
        extraction_json_pretty = _json.dumps(raw_ext, indent=2, default=str)
        if not review_table and viewed.body_text:
            from designops.pipelines.call_summary import build_review_table

            review_table = build_review_table(body=viewed.body_text, extraction=facts_only)
    if viewed and viewed.body_text:
        from designops.api.markdown_lite import markdown_lite_to_html, markdown_lite_to_plain

        body_html = markdown_lite_to_html(viewed.body_text)
        body_plain = markdown_lite_to_plain(viewed.body_text)
    if viewed is not None:
        from designops.pipelines.call_summary import email_bodies_equivalent, explain_draft_status

        draft_status = explain_draft_status(
            body_text=viewed.body_text,
            policy_blocked=bool(viewed.policy_blocked),
            policy_block_reason=viewed.policy_block_reason,
            reviewer_notes=list(viewed.reviewer_notes or []),
            transcript_quality=viewed.transcript_quality,
            low_confidence=bool(viewed.low_confidence),
            placeholder_count=int(viewed.placeholder_count or 0),
            separate_email=separate_email if isinstance(separate_email, dict) else None,
            extraction=facts_only,
        )
        # Historical placeholder drafts: treat stored body as "kept" only.
        if draft_status.get("skeleton") and not kept_body_text:
            kept_body_text = viewed.body_text
            draft_status["show_primary_email"] = False
            draft_status["show_kept_email"] = True
        same_as_followup = bool(
            kept_body_text
            and draft_status.get("show_primary_email")
            and email_bodies_equivalent(viewed.body_text, kept_body_text)
        )
        if same_as_followup or (draft_status.get("show_kept_email") and not kept_body_text):
            draft_status["show_kept_email"] = False
            kept_body_text = None
            if "kept below" in (draft_status.get("summary") or "").lower():
                draft_status["summary"] = (
                    "A follow-up email was generated (shown below). "
                    "Automatic checks flagged a few things to confirm."
                )
                draft_status["next_step"] = (
                    "Resolve the issues above, then copy subject and body."
                )
                draft_status["title"] = "Almost ready — fix these before sending"
        elif kept_body_text and not draft_status.get("skeleton"):
            draft_status["show_kept_email"] = True
        if kept_body_text and draft_status.get("show_kept_email"):
            from designops.api.markdown_lite import markdown_lite_to_html, markdown_lite_to_plain

            kept_body_html = markdown_lite_to_html(kept_body_text)
            kept_body_plain = markdown_lite_to_plain(kept_body_text)

    return templates.TemplateResponse(
        "call_summary.html",
        {
            "request": request,
            "nav": "call_summary",
            "tab": tab,
            "flash": flash,
            "flash_type": flash_type,
            "pending_tid": pending_tid,
            "pending_since": pending_since,
            "pending_ids": list(pending_ids),
            "pending_busy": pending_busy,
            "anthropic_ready": s.anthropic_configured,
            "transcript_api_ready": s.transcript_api_configured,
            "people": people,
            "include_roles_csv": ", ".join(cfg.get("include_roles") or []),
            "manual_emails_text": "\n".join(cfg.get("manual_emails") or []),
            "selected_person_ids": set(cfg.get("selected_person_ids") or []),
            "designer_count": len(designers),
            "calls": calls,
            "calls_total": calls_total,
            "call_facets": call_facets,
            "q": q,
            "filter_account": filter_account,
            "filter_designer": filter_designer,
            "filter_date_from": filter_date_from,
            "filter_date_to": filter_date_to,
            "filter_draft": filter_draft,
            "calls_qs": calls_qs,
            "page": page,
            "page_size": CALL_SUMMARY_PAGE_SIZE,
            "total_pages": total_pages,
            "drafts": drafts,
            "drafts_total": len(drafts),
            "viewed": viewed,
            "draft_id": draft_id,
            "extraction_json_pretty": extraction_json_pretty,
            "fact_sheet_pretty": fact_sheet_pretty,
            "review_table": review_table,
            "separate_email": separate_email,
            "draft_status": draft_status,
            "body_html": body_html,
            "body_plain": body_plain,
            "kept_body_html": kept_body_html,
            "kept_body_plain": kept_body_plain,
            "kept_body_text": kept_body_text,
        },
    )


@app.post("/call-summary/designers")
async def call_summary_save_designers(
    request: Request,
    db: Session = Depends(get_db),
    include_roles: str = Form(""),
    manual_emails: str = Form(""),
):
    from designops.pipelines.call_summary import save_designer_config

    form = await request.form()
    person_ids = [str(v) for v in form.getlist("person_id")]
    roles = [r.strip() for r in include_roles.split(",") if r.strip()]
    emails = [ln.strip() for ln in manual_emails.splitlines() if ln.strip()]
    save_designer_config(
        db,
        {
            "include_roles": roles,
            "manual_emails": emails,
            "selected_person_ids": person_ids,
        },
    )
    return RedirectResponse("/call-summary?tab=designers&flash=Designer+selection+saved", status_code=303)


@app.get("/call-summary/pending-status")
def call_summary_pending_status(request: Request, db: Session = Depends(get_db)):
    """Lightweight poll for in-flight draft generation (no full page reload)."""
    from fastapi.responses import JSONResponse

    tid = (request.query_params.get("pending") or "").strip()
    try:
        since = float(request.query_params.get("pending_since") or "0")
    except ValueError:
        since = 0.0
    tab = (request.query_params.get("tab") or "calls").strip().lower()
    if tab not in ("designers", "calls", "drafts"):
        tab = "calls"

    if not tid:
        return JSONResponse({"status": "idle"})

    _expire_stale_call_summary_jobs()

    err = _call_summary_failed.get(tid)
    if err:
        _call_summary_failed.pop(tid, None)
        _call_summary_pending.pop(tid, None)
        return JSONResponse(
            {
                "status": "error",
                "error": err,
                "redirect": f"/call-summary?tab={tab}&flash={quote(err)}&flash_type=error",
            }
        )

    started = _call_summary_pending.get(tid)
    if started is None:
        # Job finished and was cleared, or process restarted — check for a new draft
        latest = (
            db.query(CallSummaryDraft)
            .filter(CallSummaryDraft.transcript_id == tid)
            .order_by(CallSummaryDraft.generated_at.desc())
            .first()
        )
        if latest and latest.generated_at and since > 0 and latest.generated_at.timestamp() >= since - 2:
            return JSONResponse(
                {
                    "status": "ready",
                    "draft_id": str(latest.id),
                    "redirect": f"/call-summary?tab=drafts&draft_id={latest.id}&flash=Draft+generated",
                }
            )
        # Stale ?pending= URL with no live job — stop the spinner.
        return JSONResponse(
            {
                "status": "idle",
                "redirect": f"/call-summary?tab={tab}&flash={quote('Generation was interrupted — try Regenerate.')}&flash_type=error",
            }
        )

    latest = (
        db.query(CallSummaryDraft)
        .filter(CallSummaryDraft.transcript_id == tid)
        .order_by(CallSummaryDraft.generated_at.desc())
        .first()
    )
    if latest and latest.generated_at and latest.generated_at.timestamp() >= started - 2:
        _call_summary_pending.pop(tid, None)
        return JSONResponse(
            {
                "status": "ready",
                "draft_id": str(latest.id),
                "redirect": f"/call-summary?tab=drafts&draft_id={latest.id}&flash=Draft+generated",
            }
        )

    return JSONResponse({"status": "pending", "pending": tid, "pending_since": started})


@app.post("/call-summary/generate")
def call_summary_generate(
    transcript_id: str = Form(...),
    return_tab: str = Form("calls"),
):
    """Start draft generation in the background; stay on the requesting tab until ready."""
    import time as _time

    tab = (return_tab or "calls").strip().lower()
    if tab not in ("designers", "calls", "drafts"):
        tab = "calls"

    tid = (transcript_id or "").strip()
    if not tid:
        return RedirectResponse(
            f"/call-summary?tab={tab}&flash=Missing+transcript&flash_type=error",
            status_code=303,
        )

    _expire_stale_call_summary_jobs()
    # Surface a timed-out job for this transcript before refusing a new run.
    if tid in _call_summary_failed and tid not in _call_summary_pending:
        err = _call_summary_failed.pop(tid)
        return RedirectResponse(
            f"/call-summary?tab={tab}&flash={quote(err)}&flash_type=error",
            status_code=303,
        )

    if _call_summary_pending:
        return RedirectResponse(
            f"/call-summary?tab={tab}&flash=Already+generating+a+draft&flash_type=error",
            status_code=303,
        )

    started = _time.time()
    _call_summary_pending[tid] = started
    _call_summary_failed.pop(tid, None)

    def _bg_generate(transcript_id: str) -> None:
        from designops.core.db import session_scope
        from designops.pipelines.call_summary import generate_call_summary_draft
        import logging

        log = logging.getLogger("designops.call_summary")
        try:
            with session_scope() as s:
                result = generate_call_summary_draft(s, transcript_id)
            log.info(
                "call-summary draft ready id=%s transcript=%s blocked=%s",
                result.draft_id,
                transcript_id,
                result.policy_blocked,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("call-summary generate failed transcript=%s", transcript_id)
            _call_summary_failed[transcript_id] = str(e)[:400]
            # Keep pending until the page handler surfaces the error (same as success).
        # On success, leave _call_summary_pending until the page handler sees the
        # draft and redirects — otherwise refresh / tab switch loses Generating.

    threading.Thread(target=_bg_generate, args=(tid,), daemon=True).start()
    return RedirectResponse(
        f"/call-summary?tab={tab}&pending={quote(tid)}&pending_since={started:.3f}",
        status_code=303,
    )


# --- Design intake ------------------------------------------------------------

_intake_pending: dict[str, float] = {}
_intake_failed: dict[str, str] = {}
_intake_completed: dict[str, str] = {}  # job_id → draft_id


@app.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request, db: Session = Depends(get_db)):
    from designops.pipelines.intake import SAMPLE_INTAKE, render_preview_html

    s = get_settings()
    tab = (request.query_params.get("tab") or "input").strip().lower()
    if tab not in ("input", "drafts", "published"):
        tab = "input"

    url_pending = (request.query_params.get("pending") or "").strip()
    try:
        url_pending_since = float(request.query_params.get("pending_since") or "0")
    except ValueError:
        url_pending_since = 0.0

    completed_redirect: RedirectResponse | None = None
    for job_id, _started in list(_intake_pending.items()):
        err = _intake_failed.pop(job_id, None)
        if err:
            _intake_pending.pop(job_id, None)
            completed_redirect = RedirectResponse(
                f"/intake?tab={tab}&flash={quote(err)}&flash_type=error",
                status_code=303,
            )
            continue
        done_draft = _intake_completed.pop(job_id, None)
        if done_draft:
            _intake_pending.pop(job_id, None)
            completed_redirect = RedirectResponse(
                f"/intake?tab=drafts&draft_id={done_draft}&flash=Draft+generated",
                status_code=303,
            )
    if url_pending and url_pending in _intake_failed:
        err = _intake_failed.pop(url_pending)
        return RedirectResponse(
            f"/intake?tab={tab}&flash={quote(err)}&flash_type=error",
            status_code=303,
        )
    if completed_redirect is not None:
        return completed_redirect

    pending_ids = set(_intake_pending.keys())
    pending_job = None
    pending_since = None
    if pending_ids:
        pending_job, pending_since = max(
            ((j, _intake_pending[j]) for j in pending_ids),
            key=lambda kv: kv[1],
        )

    flash = request.query_params.get("flash")
    flash_type = request.query_params.get("flash_type") or "ok"
    draft_id = (request.query_params.get("draft_id") or "").strip()

    drafts = (
        db.query(IntakeDraft)
        .filter(IntakeDraft.status.in_(["draft", "error"]))
        .order_by(IntakeDraft.generated_at.desc())
        .limit(50)
        .all()
    )
    published = (
        db.query(IntakeDraft)
        .filter_by(status="published")
        .order_by(IntakeDraft.published_at.desc())
        .limit(50)
        .all()
    )

    notion_pages: list[dict] = []
    notion_error = None
    notion_parent_url = None
    if s.notion_parent_page_id:
        from designops.adapters.notion import _page_url

        notion_parent_url = _page_url(s.notion_parent_page_id)
    if s.notion_configured:
        try:
            from designops.adapters.notion import NotionClient

            notion_pages = NotionClient().list_intake_pages()
        except Exception as e:  # noqa: BLE001
            notion_error = str(e)[:240]

    by_notion_id: dict[str, IntakeDraft] = {}
    for d in published:
        if d.notion_page_id:
            by_notion_id[d.notion_page_id.replace("-", "").lower()] = d
    published_rows: list[dict] = []
    matched_ids: set[str] = set()
    for page in notion_pages:
        key = (page.get("id") or "").replace("-", "").lower()
        draft = by_notion_id.get(key)
        if draft:
            matched_ids.add(str(draft.id))
        published_rows.append(
            {
                "title": page.get("name") or "Untitled",
                "url": page.get("url"),
                "notion_id": page.get("id"),
                "source": page.get("source"),
                "last_edited_time": page.get("last_edited_time"),
                "draft": draft,
            }
        )
    # Local published records with no matching Notion page (orphans)
    for d in published:
        if str(d.id) in matched_ids:
            continue
        published_rows.append(
            {
                "title": d.title or "Untitled",
                "url": d.notion_page_url,
                "notion_id": d.notion_page_id,
                "source": "local",
                "last_edited_time": None,
                "draft": d,
                "missing_on_notion": True,
            }
        )

    viewed = None
    preview_html = ""
    if draft_id:
        try:
            viewed = db.get(IntakeDraft, __import__("uuid").UUID(draft_id))
        except Exception:  # noqa: BLE001
            viewed = None
    if tab == "drafts" and not viewed and drafts:
        viewed = drafts[0]
    if viewed and viewed.sections_json:
        preview_html = render_preview_html(viewed.sections_json)

    return templates.TemplateResponse(
        "intake.html",
        {
            "request": request,
            "nav": "intake",
            "tab": tab,
            "flash": flash,
            "flash_type": flash_type,
            "anthropic_ready": s.anthropic_configured,
            "notion_ready": s.notion_configured,
            "drafts": drafts,
            "published": published,
            "published_rows": published_rows,
            "notion_error": notion_error,
            "notion_parent_url": notion_parent_url,
            "viewed": viewed,
            "preview_html": preview_html,
            "pending_job": pending_job,
            "pending_since": pending_since,
            "pending_busy": bool(pending_ids),
            "sample_intake": SAMPLE_INTAKE,
        },
    )


@app.get("/intake/pending-status")
def intake_pending_status(request: Request, db: Session = Depends(get_db)):
    job_id = (request.query_params.get("pending") or "").strip()
    tab = (request.query_params.get("tab") or "input").strip()
    try:
        since = float(request.query_params.get("pending_since") or "0")
    except ValueError:
        since = 0.0

    if not job_id:
        return JSONResponse({"status": "idle"})

    err = _intake_failed.get(job_id)
    if err:
        _intake_failed.pop(job_id, None)
        _intake_pending.pop(job_id, None)
        return JSONResponse(
            {
                "status": "error",
                "error": err,
                "redirect": f"/intake?tab={tab}&flash={quote(err)}&flash_type=error",
            }
        )

    started = _intake_pending.get(job_id)
    if started is None:
        done = _intake_completed.get(job_id)
        if done:
            _intake_completed.pop(job_id, None)
            return JSONResponse(
                {
                    "status": "ready",
                    "draft_id": done,
                    "redirect": f"/intake?tab=drafts&draft_id={done}&flash=Draft+generated",
                }
            )
        return JSONResponse({"status": "idle"})

    done = _intake_completed.get(job_id)
    if done:
        _intake_pending.pop(job_id, None)
        _intake_completed.pop(job_id, None)
        return JSONResponse(
            {
                "status": "ready",
                "draft_id": done,
                "redirect": f"/intake?tab=drafts&draft_id={done}&flash=Draft+generated",
            }
        )

    return JSONResponse({"status": "pending", "pending": job_id, "pending_since": started})


@app.post("/intake/generate")
async def intake_generate(
    request: Request,
    pasted_input: str = Form(""),
    estimate_link: str = Form(""),
    proposal_link: str = Form(""),
    estimate_rows: str = Form(""),
    corrections: str = Form(""),
    draft_id: str = Form(""),
    return_tab: str = Form("input"),
    files: list[UploadFile] = File(default=[]),
):
    import time as _time
    import uuid as _uuid

    from designops.pipelines.intake import parse_spreadsheet

    tab = (return_tab or "input").strip().lower()
    if tab not in ("input", "drafts", "published"):
        tab = "input"

    if not (pasted_input or "").strip():
        return RedirectResponse(
            f"/intake?tab={tab}&flash=Email+content+is+required&flash_type=error",
            status_code=303,
        )
    if _intake_pending:
        return RedirectResponse(
            f"/intake?tab={tab}&flash=Already+generating&flash_type=error",
            status_code=303,
        )

    uploaded: list[dict[str, str]] = []
    for f in files:
        if not f.filename:
            continue
        content = await f.read()
        if not content:
            continue
        try:
            extracted = parse_spreadsheet(f.filename, content)
        except ValueError as e:
            return RedirectResponse(
                f"/intake?tab={tab}&flash={quote(str(e))}&flash_type=error",
                status_code=303,
            )
        uploaded.append({"filename": f.filename, "extracted_text": extracted})

    job_id = str(_uuid.uuid4())
    started = _time.time()
    _intake_pending[job_id] = started
    _intake_failed.pop(job_id, None)

    regen_id = (draft_id or "").strip() or None
    regen_uuid = None
    if regen_id:
        try:
            regen_uuid = _uuid.UUID(regen_id)
        except ValueError:
            regen_uuid = None

    def _bg(job_id: str, regen: _uuid.UUID | None) -> None:
        from designops.core.db import session_scope
        from designops.pipelines.intake import generate_intake_draft
        import logging

        log = logging.getLogger("designops.intake")
        try:
            with session_scope() as s:
                result = generate_intake_draft(
                    s,
                    pasted_input=pasted_input,
                    estimate_link=estimate_link,
                    proposal_link=proposal_link,
                    estimate_rows=estimate_rows,
                    corrections=corrections,
                    uploaded_files=uploaded,
                    draft_id=regen,
                )
            log.info("intake draft ready id=%s error=%s", result.draft_id, result.error)
            _intake_completed[job_id] = str(result.draft_id)
        except Exception as e:  # noqa: BLE001
            log.exception("intake generate failed")
            _intake_failed[job_id] = str(e)[:400]

    threading.Thread(target=_bg, args=(job_id, regen_uuid), daemon=True).start()
    return RedirectResponse(
        f"/intake?tab={tab}&pending={quote(job_id)}&pending_since={started:.3f}",
        status_code=303,
    )


@app.post("/intake/publish")
def intake_publish(
    draft_id: str = Form(...),
    db: Session = Depends(get_db),
):
    import uuid as _uuid

    from designops.pipelines.intake import publish_intake_draft

    s = get_settings()
    if not s.notion_configured:
        return RedirectResponse(
            "/intake?tab=drafts&flash=Notion+not+configured&flash_type=error",
            status_code=303,
        )
    try:
        did = _uuid.UUID(draft_id)
    except ValueError:
        return RedirectResponse(
            "/intake?tab=drafts&flash=Invalid+draft&flash_type=error",
            status_code=303,
        )
    try:
        result = publish_intake_draft(db, did)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/intake?tab=drafts&draft_id={draft_id}&flash={quote(str(e)[:200])}&flash_type=error",
            status_code=303,
        )
    return RedirectResponse(
        f"/intake?tab=published&draft_id={result.draft_id}&flash=Published+to+Notion",
        status_code=303,
    )


@app.post("/intake/delete")
def intake_delete(
    draft_id: str = Form(...),
    return_tab: str = Form("drafts"),
    db: Session = Depends(get_db),
):
    import uuid as _uuid

    tab = (return_tab or "drafts").strip().lower()
    if tab not in ("drafts", "published"):
        tab = "drafts"
    try:
        did = _uuid.UUID(draft_id)
    except ValueError:
        return RedirectResponse(
            f"/intake?tab={tab}&flash=Invalid+draft&flash_type=error",
            status_code=303,
        )
    draft = db.get(IntakeDraft, did)
    if not draft:
        return RedirectResponse(
            f"/intake?tab={tab}&flash=Draft+not+found&flash_type=error",
            status_code=303,
        )
    title = (draft.title or "Untitled").strip() or "Untitled"
    db.delete(draft)
    db.commit()
    return RedirectResponse(
        f"/intake?tab={tab}&flash={quote(f'Deleted: {title}')}",
        status_code=303,
    )


@app.get("/intake/sample-estimate.csv")
def intake_sample_estimate_csv():
    path = Path(__file__).resolve().parents[1] / "seeds" / "intake_sample_estimate.csv"
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=intake_sample_estimate.csv"},
    )


def _bg_execute_run(run_id: str, reuse_ingest: bool) -> None:
    """Background worker for *manual* Generate buttons.

    Always forces send_mode=none — Delivery on the schedule card applies only to
    the cron job. Email a finished run from the run page if needed.
    Wraps the call in a top-level try/except so that if the thread crashes for
    any reason (including uvicorn reload killing the process), the run is marked
    as failed rather than stuck in 'running' forever.
    """
    from designops.core.db import session_scope
    from designops.pipelines.daily_digest import execute_run as execute_daily
    from designops.pipelines.weekly_backlog import execute_run as execute_weekly
    from designops.pipelines.weekly_health import execute_run as execute_weekly_health

    try:
        with session_scope() as s:
            run = s.get(PipelineRun, run_id)
            if run is None:
                return
            pipe = s.get(Pipeline, run.pipeline_id)
            kw = {"reuse_ingest": reuse_ingest, "send_mode_override": SendMode.NONE.value}
            if pipe and pipe.key == WEEKLY_KEY:
                execute_weekly(s, run, **kw)
            elif pipe and pipe.key == WEEKLY_HEALTH_KEY:
                execute_weekly_health(s, run, **kw)
            else:
                execute_daily(s, run, **kw)
    except Exception:
        import logging, traceback
        logging.getLogger(__name__).error(
            "bg run %s crashed: %s", run_id, traceback.format_exc()
        )
        try:
            from datetime import datetime as _dt, timezone as _tz
            with session_scope() as s2:
                r = s2.get(PipelineRun, run_id)
                if r and r.status == "running":
                    r.status = "failed"
                    r.error = traceback.format_exc()[-500:]
                    r.finished_at = _dt.now(_tz.utc)
                    s2.commit()
        except Exception:
            pass


@app.post("/daily-report/run")
def daily_report_run(
    report_date: str = Form(...),
    reuse_ingest: str = Form("on"),
    db: Session = Depends(get_db),
):
    # Create the run NOW (status=running) and commit, so it appears in history
    # immediately; the heavy export + synthesis happens in the background.
    rd = date.fromisoformat(report_date)
    run = create_pending_run(db, rd)
    db.commit()
    run_id = str(run.id)
    threading.Thread(
        target=_bg_execute_run, args=(run_id, reuse_ingest == "on"), daemon=True
    ).start()
    return RedirectResponse(f"/runs/{run_id}", status_code=302)


# Call summaries (/call-summary) and Design intake (/intake) keep their DB rows
# for those dedicated screens; they are not pipeline-config cards.
_PIPELINES_PAGE_HIDDEN_KEYS = frozenset({"call-summary", "design-intake", "intake"})


def _hidden_from_pipelines_page(key: str | None) -> bool:
    k = (key or "").strip().lower().replace("_", "-")
    return k in _PIPELINES_PAGE_HIDDEN_KEYS


# --- Pipelines ----------------------------------------------------------------
@app.get("/pipelines", response_class=HTMLResponse)
def pipelines(request: Request, db: Session = Depends(get_db)):
    from designops.api.scheduler import (
        describe_schedule,
        format_countdown,
        next_run_time,
        scheduled_report_date,
    )

    rows = db.query(Pipeline).order_by(Pipeline.key).all()
    data = []
    for p in rows:
        if _hidden_from_pipelines_page(p.key):
            continue
        last = (
            db.query(PipelineRun)
            .filter_by(pipeline_id=p.id)
            .order_by(PipelineRun.started_at.desc())
            .first()
        )
        sched_active = bool(p.enabled and p.schedule_cron)
        nrt = next_run_time(p.key) if sched_active else None
        report_day = scheduled_report_date(p.key, nrt) if nrt else None
        covers = None
        if report_day is not None:
            covers = report_day.strftime("%a %-d %b").replace(" 0", " ")
        note = None
        if p.key == "daily-digest":
            note = (
                "Weekdays only. Each run covers the previous working day — "
                "Monday sends Friday’s report."
            )
        data.append(
            {
                "p": p,
                "last": last,
                "sched_active": sched_active,
                "sched_desc": describe_schedule(p.schedule_cron, p.timezone),
                "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
                "sched_countdown": format_countdown(nrt),
                "sched_covers": covers,
                "sched_note": note,
                "sched_delivery": _pipeline_schedule_delivery(p),
            }
        )
    return templates.TemplateResponse(
        "pipelines.html", {"request": request, "pipelines": data, "nav": "pipelines"}
    )


@app.post("/pipelines/{key}")
def update_pipeline(
    key: str,
    schedule_cron: str = Form(""),
    recipients: str = Form(""),
    send_mode: str = Form("none"),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
):
    p = db.query(Pipeline).filter_by(key=key).one_or_none()
    if not p:
        raise HTTPException(404)
    p.schedule_cron = schedule_cron or None
    p.recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    p.send_mode = SendMode(send_mode).value
    p.enabled = enabled == "on"
    # go_live is NOT editable here — it is a deliberate promotion gate (§12.4).
    db.add(p)
    db.commit()
    from designops.api.scheduler import reschedule

    reschedule()
    return RedirectResponse("/pipelines", status_code=302)


@app.post("/pipelines/{key}/run")
def run_pipeline(
    key: str,
    report_date: str | None = Form(None),
    reuse_ingest: str = Form("on"),
    db: Session = Depends(get_db),
):
    if key != WEEKLY_HEALTH_KEY and not report_date:
        raise HTTPException(422, "report_date is required")
    rd = date.fromisoformat(report_date) if report_date else date.today()
    if key == "daily-digest":
        run = create_pending_run(db, rd)
    elif key == WEEKLY_KEY:
        run = create_weekly_pending_run(db, resolve_week_monday(rd))
    elif key == WEEKLY_HEALTH_KEY:
        # Health is a snapshot: always reports the latest state as of today.
        run = create_weekly_health_pending_run(db)
    else:
        raise HTTPException(404, f"pipeline {key!r} is not runnable")
    db.commit()
    run_id = str(run.id)
    threading.Thread(
        target=_bg_execute_run, args=(run_id, reuse_ingest == "on"), daemon=True
    ).start()
    return RedirectResponse(f"/runs/{run_id}", status_code=302)


@app.get("/weekly-backlog", response_class=HTMLResponse)
def weekly_backlog_page(request: Request, db: Session = Depends(get_db)):
    pipeline = db.query(Pipeline).filter_by(key=WEEKLY_KEY).one_or_none()
    recent = (
        db.query(PipelineRun)
        .filter_by(pipeline_id=pipeline.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(20)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    dedicated = (
        db.query(Person)
        .filter(Person.is_dedicated.is_(True))
        .order_by(Person.full_name)
        .all()
    )
    sched_time, _ = _cron_to_fields(pipeline.schedule_cron if pipeline else "0 11 * * mon")
    sched_weekday = _cron_weekday(pipeline.schedule_cron if pipeline else "0 11 * * mon")
    from designops.api.scheduler import next_run_time

    nrt = next_run_time(WEEKLY_KEY)
    # Default week_of = this week's Monday (Mon–Fri) or next Monday (weekend)
    default_monday = resolve_week_monday(date.today())
    return templates.TemplateResponse(
        "weekly_backlog.html",
        {
            "request": request,
            "nav": "weekly",
            "pipeline": pipeline,
            "recent": recent,
            "default_week": default_monday.isoformat(),
            "anthropic_ready": s.anthropic_configured,
            "jira_ready": s.jira_configured,
            "sched_enabled": bool(pipeline and pipeline.enabled),
            "sched_time": sched_time,
            "sched_weekday": sched_weekday,
            "sched_recipients": ", ".join(pipeline.recipients) if pipeline else "",
            "sched_send": (pipeline.send_mode if pipeline else "none"),
            "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
            "normal_hours": s.normal_week_hours,
            "dedicated": dedicated,
        },
    )


@app.post("/weekly-backlog/run")
def weekly_backlog_run(
    week_of: str = Form(...),
    reuse_ingest: str = Form("on"),
    db: Session = Depends(get_db),
):
    rd = resolve_week_monday(date.fromisoformat(week_of))
    run = create_weekly_pending_run(db, rd)
    db.commit()
    run_id = str(run.id)
    threading.Thread(
        target=_bg_execute_run, args=(run_id, reuse_ingest == "on"), daemon=True
    ).start()
    return RedirectResponse(f"/runs/{run_id}", status_code=302)


@app.post("/weekly-backlog/schedule")
def weekly_backlog_schedule(
    sched_time: str = Form("11:00"),
    weekday: str = Form("mon"),
    recipients: str = Form(""),
    send_mode: str = Form("none"),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
):
    from designops.api.scheduler import reschedule

    p = db.query(Pipeline).filter_by(key=WEEKLY_KEY).one()
    try:
        h, m = (int(x) for x in sched_time.split(":")[:2])
    except (ValueError, TypeError):
        h, m = 11, 0
    dow = _CRON_DOW_ALIASES.get((weekday or "").strip().lower(), "mon")
    if dow not in _CRON_WEEKDAYS:
        dow = "mon"
    p.schedule_cron = f"{m} {h} * * {dow}"
    p.recipients = [
        r.strip() for r in recipients.replace("\n", ",").split(",") if r.strip()
    ]
    p.send_mode = send_mode if send_mode in ("none", "self", "draft", "send") else "none"
    p.enabled = enabled == "on"
    db.add(p)
    db.commit()
    reschedule()
    return RedirectResponse("/weekly-backlog?flash=schedule_saved", status_code=302)


@app.get("/weekly-health", response_class=HTMLResponse)
def weekly_health_page(request: Request, db: Session = Depends(get_db)):
    pipeline = db.query(Pipeline).filter_by(key=WEEKLY_HEALTH_KEY).one_or_none()
    recent = (
        db.query(PipelineRun)
        .filter_by(pipeline_id=pipeline.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(20)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    sched_time, _ = _cron_to_fields(pipeline.schedule_cron if pipeline else "0 12 * * tue")
    sched_weekday = _cron_weekday(
        pipeline.schedule_cron if pipeline else "0 12 * * tue", default="tue"
    )
    from designops.api.scheduler import next_run_time

    nrt = next_run_time(WEEKLY_HEALTH_KEY)
    tracked = (
        db.query(Project)
        .filter_by(track_weekly_health=True, active=True)
        .order_by(Project.canonical_name)
        .all()
    )
    projects_by_fw = {
        p.fairwind_account_id: p
        for p in db.query(Project).filter(Project.fairwind_account_id.isnot(None)).all()
        if p.fairwind_account_id
    }
    accounts_payload = []
    digest_by_fw: dict[str, bool] = {}
    acct_by_fw: dict[str, Account] = {}
    for a in db.query(Account).order_by(Account.name).all():
        acct_by_fw[a.fairwind_account_id] = a
        digest_by_fw[a.fairwind_account_id] = bool(a.digest_enabled)
        proj = projects_by_fw.get(a.fairwind_account_id)
        accounts_payload.append(
            {
                "id": a.fairwind_account_id,
                "name": a.name,
                "domains": list(a.domains or []),
                "jira": list(a.jira_project_keys or [])[:4],
                "active": bool(a.is_active),
                "tracked": bool(proj and proj.track_weekly_health),
                "project_id": str(proj.id) if proj else None,
            }
        )
    export_to = _prev_working_day(date.today())
    export_from = export_to - timedelta(days=6)
    for t in tracked:
        acct = acct_by_fw.get(t.fairwind_account_id or "")
        t.digest_enabled = bool(digest_by_fw.get(t.fairwind_account_id or "", False))
        t.digest_account_id = t.fairwind_account_id if t.fairwind_account_id else None
        t.acct_domain = (acct.domains[0] if acct and acct.domains else None)
        t.acct_synced = acct.synced_at if acct else None
        t.acct_keys = list(acct.jira_project_keys or []) if acct else []
        t.acct_notes = (acct.notes if acct else None) or t.notes
    return templates.TemplateResponse(
        "weekly_health.html",
        {
            "request": request,
            "nav": "weekly_health",
            "pipeline": pipeline,
            "recent": recent,
            "tracked": tracked,
            "accounts_json": accounts_payload,
            "export_from": export_from.isoformat(),
            "export_to": export_to.isoformat(),
            "fairwind_ready": s.fairwind_configured,
            "today_label": date.today().strftime("%a %-d %b %Y"),
            "anthropic_ready": s.anthropic_configured,
            "jira_ready": s.jira_configured,
            "sched_enabled": bool(pipeline and pipeline.enabled),
            "sched_time": sched_time,
            "sched_weekday": sched_weekday,
            "sched_recipients": ", ".join(pipeline.recipients) if pipeline else "",
            "sched_send": (pipeline.send_mode if pipeline else "none"),
            "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
            "flash_name": request.query_params.get("name"),
        },
    )


def _account_from_fairwind_row(db: Session, row: dict) -> Account:
    """Upsert a local Account from a Fairwind directory row (directory columns only)."""
    fid = str(row.get("id") or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="Fairwind account missing id")
    acct = db.query(Account).filter_by(fairwind_account_id=fid).one_or_none()
    if acct is None:
        acct = Account(
            fairwind_account_id=fid,
            name=(row.get("name") or "").strip() or "(unnamed)",
            is_active=bool(row.get("is_active")),
            domains=list(row.get("domains") or []),
            jira_project_keys=[],
            salesforce_account_ids=[],
            notion_space_ids=[],
            data_availability=row.get("data_availability") or {},
            aliases=[],
            digest_enabled=False,
        )
        db.add(acct)
    else:
        acct.name = (row.get("name") or "").strip() or acct.name
        if "is_active" in row:
            acct.is_active = bool(row.get("is_active"))
        if row.get("domains") is not None:
            acct.domains = list(row.get("domains") or [])
    # jira keys may arrive as [{key:…}] or strings
    jp = row.get("jira_projects") or row.get("jira_project_keys")
    if jp:
        keys: list[str] = []
        for it in jp:
            if isinstance(it, dict) and it.get("key"):
                keys.append(str(it["key"]))
            elif isinstance(it, str) and it.strip():
                keys.append(it.strip())
        if keys:
            acct.jira_project_keys = keys
    db.flush()
    return acct


def _track_project_for_account(db: Session, account: Account) -> Project:
    from designops.core.projects import ensure_project_for_account, resolve_jira_candidates

    project = ensure_project_for_account(db, account)
    project.track_weekly_health = True
    project.active = True
    # Auto-link Jira when missing: account keys → Fairwind map → Jira Cloud search.
    if not (project.jira_project_key or "").strip():
        fw_rows: list[dict] = []
        jira_rows: list[dict] = []
        s = get_settings()
        if s.fairwind_configured:
            try:
                fw_rows = FairwindClient(s).list_jira_projects()
            except Exception:  # noqa: BLE001 — best-effort on add
                fw_rows = []
        if s.jira_configured:
            try:
                from designops.adapters.jira import JiraClient

                jira_rows = JiraClient(s).search_projects(account.name or project.canonical_name)
            except Exception:  # noqa: BLE001
                jira_rows = []
        matches = resolve_jira_candidates(
            project_name=project.canonical_name,
            fairwind_account_id=account.fairwind_account_id,
            account_keys=list(account.jira_project_keys or []),
            fairwind_jira_projects=fw_rows,
            jira_cloud_projects=jira_rows,
        )
        # Auto-apply only a clear top match (exact / account-linked).
        if matches and matches[0]["score"] <= 1:
            project.jira_project_key = matches[0]["key"]
    db.add(project)
    return project


def _jira_candidates_for_project(db: Session, project: Project) -> tuple[list[dict], list[str]]:
    from designops.adapters.jira import JiraClient
    from designops.core.models import Person
    from designops.core.projects import resolve_jira_candidates
    from designops.pipelines.weekly_health_math import (
        design_roster_emails,
        enrich_jira_candidates_with_design_fit,
    )

    account = None
    if project.fairwind_account_id:
        account = (
            db.query(Account)
            .filter_by(fairwind_account_id=project.fairwind_account_id)
            .one_or_none()
        )
    s = get_settings()
    fw_rows: list[dict] = []
    jira_rows: list[dict] = []
    sources: list[str] = []
    jira_client = None
    if s.fairwind_configured:
        try:
            fw_rows = FairwindClient(s).list_jira_projects()
            sources.append("fairwind")
        except Exception as e:  # noqa: BLE001
            sources.append(f"fairwind_error:{e}")
    if s.jira_configured:
        try:
            jira_client = JiraClient(s)
            jira_rows = jira_client.search_projects(project.canonical_name)
            sources.append("jira")
        except Exception as e:  # noqa: BLE001
            sources.append(f"jira_error:{e}")
    matches = resolve_jira_candidates(
        project_name=project.canonical_name,
        fairwind_account_id=project.fairwind_account_id,
        account_keys=list((account.jira_project_keys if account else None) or []),
        fairwind_jira_projects=fw_rows,
        jira_cloud_projects=jira_rows,
    )
    if jira_client is not None and matches:
        roster = list(db.query(Person).filter(Person.status != "out").all())
        matches = enrich_jira_candidates_with_design_fit(
            matches,
            client=jira_client,
            roster_emails=design_roster_emails(roster),
        )
        sources.append("design_fit")
    return matches, sources


@app.post("/weekly-health/verify-account")
async def weekly_health_verify_account(request: Request):
    """Live Fairwind account search when the local directory has no match."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    q = str((body or {}).get("q") or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Type at least 2 characters")
    s = get_settings()
    if not s.fairwind_configured:
        raise HTTPException(status_code=400, detail="Fairwind credentials not configured")
    try:
        client = FairwindClient(s)
        rows = client.list_accounts()
    except FairwindError as e:
        raise HTTPException(status_code=502, detail=f"Fairwind error: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Fairwind error: {e}") from e

    ql = q.lower()
    scored: list[tuple[int, dict]] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name or not r.get("id"):
            continue
        nl = name.lower()
        if ql == nl:
            score = 0
        elif nl.startswith(ql):
            score = 1
        elif ql in nl:
            score = 2
        else:
            domains = " ".join(r.get("domains") or []).lower()
            jira = " ".join(
                str(it.get("key") if isinstance(it, dict) else it)
                for it in (r.get("jira_projects") or [])
            ).lower()
            if ql not in domains and ql not in jira:
                continue
            score = 3
        jira_keys = []
        for it in r.get("jira_projects") or []:
            if isinstance(it, dict) and it.get("key"):
                jira_keys.append(str(it["key"]))
            elif isinstance(it, str):
                jira_keys.append(it)
        scored.append(
            (
                score,
                {
                    "id": str(r["id"]),
                    "name": name,
                    "domains": list(r.get("domains") or [])[:4],
                    "jira": jira_keys[:4],
                    "active": bool(r.get("is_active")),
                    "jira_projects": r.get("jira_projects") or [],
                    "is_active": bool(r.get("is_active")),
                    "data_availability": r.get("data_availability") or {},
                },
            )
        )
    scored.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    matches = [m for _, m in scored[:8]]
    return JSONResponse(
        {
            "query": q,
            "matches": matches,
            "count": len(matches),
            "searched": len(rows),
        }
    )


@app.post("/weekly-health/confirm-account")
async def weekly_health_confirm_account(request: Request, db: Session = Depends(get_db)):
    """After Fairwind verify: upsert account, link project, add to weekly health."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not body or not body.get("id"):
        raise HTTPException(status_code=400, detail="Fairwind account id required")
    acct = _account_from_fairwind_row(db, body)
    project = _track_project_for_account(db, acct)
    return JSONResponse(
        {
            "ok": True,
            "name": project.canonical_name,
            "project_id": str(project.id),
            "redirect": f"/weekly-health?flash=tracked&name={quote(project.canonical_name)}",
        }
    )


@app.post("/weekly-health/track-account")
def weekly_health_track_account(
    fairwind_account_id: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Track weekly health for a local Fairwind account (create/link project if needed)."""
    fid = (fairwind_account_id or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="fairwind_account_id required")
    acct = db.query(Account).filter_by(fairwind_account_id=fid).one_or_none()
    if acct is None:
        display = (name or "").strip() or "(unnamed)"
        acct = Account(
            fairwind_account_id=fid,
            name=display,
            is_active=True,
            domains=[],
            jira_project_keys=[],
            salesforce_account_ids=[],
            notion_space_ids=[],
            data_availability={},
            aliases=[],
            digest_enabled=False,
        )
        db.add(acct)
        db.flush()
    project = _track_project_for_account(db, acct)
    return RedirectResponse(
        f"/weekly-health?flash=tracked&name={quote(project.canonical_name)}",
        status_code=302,
    )


@app.post("/weekly-health/projects/{project_id}/track")
def weekly_health_track_project(
    project_id: str,
    enable: str = Form(...),
    db: Session = Depends(get_db),
):
    """Add or remove a project from the weekly-health allowlist."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    on = enable == "on"
    project.track_weekly_health = on
    db.add(project)
    flash = "tracked" if on else "untracked"
    return RedirectResponse(
        f"/weekly-health?flash={flash}&name={quote(project.canonical_name)}",
        status_code=302,
    )


@app.post("/weekly-health/projects/{project_id}")
def weekly_health_update_project(
    project_id: str,
    signed_design_estimate_h: str = Form(""),
    display_subtitle: str = Form(""),
    jira_project_key: str = Form(""),
    figma_urls: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit weekly-health fields on a tracked project (estimate + subtitle + Jira + Figma)."""
    from designops.adapters.figma import FigmaError, parse_figma_url_list

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    raw = (signed_design_estimate_h or "").strip()
    if not raw:
        project.signed_design_estimate_h = None
    else:
        try:
            project.signed_design_estimate_h = float(raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="signed estimate must be a number") from e
    project.display_subtitle = (display_subtitle or "").strip() or None
    key = (jira_project_key or "").strip().upper() or None
    project.jira_project_key = key
    try:
        project.figma_urls = parse_figma_url_list(figma_urls)
    except FigmaError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.add(project)
    return RedirectResponse(
        f"/weekly-health?flash=project_saved&name={quote(project.canonical_name)}",
        status_code=302,
    )


@app.post("/weekly-health/projects/{project_id}/verify-jira")
def weekly_health_verify_jira(project_id: str, db: Session = Depends(get_db)):
    """Find Jira project key candidates for a tracked project (Fairwind + Jira Cloud).

    Multiple keys are returned ranked by design-ticket fit (design roster /
    Design·UX component). Auto-link only when a single clear name match remains.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    s = get_settings()
    if not s.fairwind_configured and not s.jira_configured:
        raise HTTPException(
            status_code=400,
            detail="Configure Fairwind or Jira credentials to verify project keys",
        )
    matches, sources = _jira_candidates_for_project(db, project)
    auto = None
    if len(matches) == 1 and matches[0].get("score", 99) <= 1:
        auto = matches[0]
        project.jira_project_key = auto["key"]
        db.add(project)
        db.commit()
    return JSONResponse(
        {
            "project_id": str(project.id),
            "name": project.canonical_name,
            "current_key": project.jira_project_key,
            "auto_applied": auto,
            "matches": matches[:12],
            "sources": sources,
        }
    )


@app.post("/weekly-health/projects/{project_id}/set-jira")
async def weekly_health_set_jira(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Set / confirm a Jira project key after verify."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = str((body or {}).get("key") or "").strip().upper()
    if not key:
        raise HTTPException(status_code=400, detail="Jira key required")
    # Optional live check when Jira is configured
    s = get_settings()
    if s.jira_configured:
        try:
            from designops.adapters.jira import JiraClient

            found = JiraClient(s).get_project(key)
            if found is None:
                raise HTTPException(status_code=404, detail=f"Jira project {key} not found")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Jira lookup failed: {e}") from e
    project.jira_project_key = key
    db.add(project)
    return JSONResponse(
        {
            "ok": True,
            "key": key,
            "name": project.canonical_name,
            "redirect": f"/weekly-health?flash=jira_linked&name={quote(project.canonical_name)}",
        }
    )


@app.post("/weekly-health/projects/{project_id}/fetch-figma")
def weekly_health_fetch_figma(project_id: str, db: Session = Depends(get_db)):
    """Find Figma file URLs via Jira Cloud + Fairwind corpus; enrich with last updated."""
    from designops.adapters import figma as figma_api
    from designops.adapters.jira import JiraClient
    from designops.pipelines.call_summary_links import (
        harvest_figma_urls_from_fairwind,
        harvest_figma_urls_from_jira,
        merge_figma_url_hits,
    )

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    s = get_settings()
    if not s.jira_configured and not s.fairwind_configured:
        raise HTTPException(
            status_code=400,
            detail="Configure Jira and/or Fairwind credentials to fetch Figma links",
        )

    keys: list[str] = []
    sources: list[str] = []
    errors: list[str] = []
    if project.jira_project_key:
        keys.append(project.jira_project_key)
        sources.append("project_key")

    account = None
    if project.fairwind_account_id:
        account = (
            db.query(Account)
            .filter_by(fairwind_account_id=project.fairwind_account_id)
            .one_or_none()
        )
    if account and account.jira_project_keys:
        for k in account.jira_project_keys:
            if k and str(k).upper() not in keys:
                keys.append(str(k).upper())
        sources.append("fairwind_account")

    # If still no keys, try the same candidate resolver as Verify Jira
    if not keys and (s.jira_configured or s.fairwind_configured):
        matches, cand_sources = _jira_candidates_for_project(db, project)
        sources.extend(cand_sources)
        for m in matches[:5]:
            k = (m.get("key") or "").strip().upper()
            if k and k not in keys:
                keys.append(k)
        if matches:
            sources.append("jira_candidates")

    jira_hits: list[dict] = []
    fw_hits: list[dict] = []

    if s.jira_configured and keys:
        try:
            result = harvest_figma_urls_from_jira(keys, client=JiraClient(s))
            jira_hits = result.get("urls") or []
            errors.extend(result.get("errors") or [])
            sources.append("jira_search")
        except Exception as e:  # noqa: BLE001
            errors.append(f"jira: {e}")
    elif s.jira_configured and not keys:
        errors.append("jira: no project key to search")

    if project.fairwind_account_id and s.fairwind_configured:
        try:
            fw = harvest_figma_urls_from_fairwind(
                project.fairwind_account_id,
                jira_keys=keys or None,
            )
            fw_hits = fw.get("urls") or []
            errors.extend(fw.get("errors") or [])
            sources.append("fairwind_corpus")
        except Exception as e:  # noqa: BLE001
            errors.append(f"fairwind: {e}")
    elif not project.fairwind_account_id:
        errors.append("fairwind: project has no Fairwind account link")

    urls = merge_figma_url_hits(jira_hits, fw_hits)
    if not urls and not keys and not project.fairwind_account_id:
        raise HTTPException(
            status_code=400,
            detail="No Jira key or Fairwind account — link one first",
        )

    existing = {u.split("?")[0].rstrip("/") for u in (project.figma_urls or [])}
    urls = figma_api.enrich_urls_with_meta(urls, settings=s)
    for row in urls:
        row["already_saved"] = row.get("url") in existing
        # Convenience single source label for the UI
        srcs = row.get("sources") or ([row["source"]] if row.get("source") else [])
        row["source_label"] = "+".join(srcs)

    return JSONResponse(
        {
            "project_id": str(project.id),
            "name": project.canonical_name,
            "keys_searched": keys,
            "sources": sources,
            "errors": errors,
            "urls": urls,
            "existing": list(project.figma_urls or []),
            "figma_meta_ready": figma_api.is_ready(s),
        }
    )


@app.post("/weekly-health/run")
def weekly_health_run(
    reuse_ingest: str = Form("on"),
    db: Session = Depends(get_db),
):
    # Snapshot report — always as of today, no week picker.
    run = create_weekly_health_pending_run(db)
    db.commit()
    run_id = str(run.id)
    threading.Thread(
        target=_bg_execute_run, args=(run_id, reuse_ingest == "on"), daemon=True
    ).start()
    return RedirectResponse(f"/runs/{run_id}", status_code=302)


@app.post("/weekly-health/schedule")
def weekly_health_schedule(
    sched_time: str = Form("12:00"),
    weekday: str = Form("tue"),
    recipients: str = Form(""),
    send_mode: str = Form("none"),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
):
    from designops.api.scheduler import reschedule

    p = db.query(Pipeline).filter_by(key=WEEKLY_HEALTH_KEY).one()
    try:
        h, m = (int(x) for x in sched_time.split(":")[:2])
    except (ValueError, TypeError):
        h, m = 12, 0
    dow = _CRON_DOW_ALIASES.get((weekday or "").strip().lower(), "tue")
    if dow not in _CRON_WEEKDAYS:
        dow = "tue"
    p.schedule_cron = f"{m} {h} * * {dow}"
    p.recipients = [
        r.strip() for r in recipients.replace("\n", ",").split(",") if r.strip()
    ]
    p.send_mode = send_mode if send_mode in ("none", "self", "draft", "send") else "none"
    p.enabled = enabled == "on"
    db.add(p)
    db.commit()
    reschedule()
    return RedirectResponse("/weekly-health?flash=schedule_saved", status_code=302)


@app.post("/daily-report/schedule")
def daily_report_schedule(
    sched_time: str = Form("12:00"),
    days: str = Form("weekdays"),
    recipients: str = Form(""),
    send_mode: str = Form("none"),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
):
    """Save the daily schedule + recipient list, then reschedule the cron job."""
    from designops.api.scheduler import reschedule

    p = db.query(Pipeline).filter_by(key="daily-digest").one()
    try:
        h, m = (int(x) for x in sched_time.split(":")[:2])
    except (ValueError, TypeError):
        h, m = 12, 0
    p.schedule_cron = f"{m} {h} * * {'*' if days == 'all' else 'mon-fri'}"
    p.recipients = [r.strip() for r in recipients.replace("\n", ",").split(",") if r.strip()]
    p.send_mode = send_mode if send_mode in ("none", "self", "draft", "send") else "none"
    p.enabled = enabled == "on"
    db.add(p)
    db.commit()
    reschedule()
    return RedirectResponse("/daily-report?flash=schedule_saved", status_code=302)


# --- Run log ------------------------------------------------------------------
@app.get("/runs", response_class=HTMLResponse)
def run_log(request: Request, db: Session = Depends(get_db)):
    runs = db.query(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(100).all()
    return templates.TemplateResponse(
        "run_log.html",
        {"request": request, "runs": runs, "nav": "runs", "today": date.today().isoformat()},
    )


# --- Artifact viewer (3 panes, §12.3) ----------------------------------------
@app.get("/runs/{run_id}", response_class=HTMLResponse)
def artifact_viewer(run_id: str, request: Request, db: Session = Depends(get_db)):
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404)
    docs = (
        db.query(RunDocument)
        .filter_by(run_id=run.id)
        .order_by(RunDocument.included.desc(), RunDocument.exclusion_reason)
        .all()
    )
    flags = db.query(Flag).filter_by(run_id=run.id).all()
    people = {p.id: p.full_name for p in db.query(Person).all()}
    projects = {p.id: p.canonical_name for p in db.query(Project).all()}
    return templates.TemplateResponse(
        "artifact.html",
        {
            "request": request, "run": run, "docs": docs, "flags": flags,
            "people": people, "projects": projects, "nav": "runs",
            "owner_email": get_settings().setup_owner_email,
            "can_send": google_oauth.is_connected() or get_settings().smtp_configured,
            "sent": request.query_params.get("sent"),
            "sent_msg": request.query_params.get("msg"),
            "sent_to": request.query_params.get("to"),
        },
    )


@app.post("/runs/{run_id}/email")
def email_run(
    run_id: str,
    recipient: str = Form(...),
    db: Session = Depends(get_db),
):
    """Send this run's digest to one or more recipients (comma/space separated),
    all on a single email."""
    from urllib.parse import quote

    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404)
    art = (
        db.query(Artifact)
        .filter_by(run_id=run_id, kind="html")
        .order_by(Artifact.id.desc())
        .first()
    )
    if not art:
        return RedirectResponse(f"/runs/{run_id}?sent=error&msg=no+digest+yet", status_code=302)
    recipients = [
        r for r in re.split(r"[,;\s]+", recipient.strip()) if r
    ]
    if not recipients or any("@" not in r for r in recipients):
        return RedirectResponse(
            f"/runs/{run_id}?sent=error&msg=enter+valid+emails+separated+by+commas",
            status_code=302,
        )
    pipeline = db.get(Pipeline, run.pipeline_id)
    result = send_digest(
        recipients,
        email_subject_for_pipeline(
            pipeline.key if pipeline else "",
            run.report_date,
        ),
        art.content,
        status_label="sent",
    )
    if result.status != "failed":
        art.delivery_status = result.status
        art.delivered_at = datetime.now()
        art.message_id = result.message_id
        db.add(art)
        return RedirectResponse(
            f"/runs/{run_id}?sent=ok&to={quote(', '.join(recipients))}", status_code=302
        )
    return RedirectResponse(
        f"/runs/{run_id}?sent=error&msg={quote(result.note or 'failed')}", status_code=302
    )


# --- Google OAuth (Connect Gmail for sending + CRO mailbox read) -------------
@app.get("/settings/google/connect")
def google_connect():
    s = get_settings()
    if not s.google_oauth_configured:
        return RedirectResponse("/config?google=notconfigured", status_code=302)
    url = google_oauth.build_auth_url(state=google_oauth.STATE_DELIVERY, settings=s)
    return RedirectResponse(url, status_code=302)


@app.get("/settings/google/cro/connect")
def google_cro_connect():
    s = get_settings()
    if not s.google_oauth_configured:
        return RedirectResponse("/config?google_cro=notconfigured", status_code=302)
    url = google_oauth.build_cro_auth_url(settings=s)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/google/callback")
def google_callback(code: str = "", error: str = "", state: str = ""):
    from urllib.parse import quote

    is_cro = (state or "").strip().lower() == google_oauth.STATE_CRO
    flash_key = "google_cro" if is_cro else "google"
    dest = "/config"
    if error or not code:
        return RedirectResponse(
            f"{dest}?{flash_key}=error&msg={quote(error or 'no code')}", status_code=302
        )
    try:
        if is_cro:
            google_oauth.exchange_cro_code(code, settings=get_settings())
        else:
            google_oauth.exchange_code(code, settings=get_settings())
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"{dest}?{flash_key}=error&msg={quote(f'{type(e).__name__}: {e}')}",
            status_code=302,
        )
    return RedirectResponse(f"{dest}?{flash_key}=connected", status_code=302)


@app.post("/settings/google/disconnect")
def google_disconnect():
    google_oauth.disconnect(get_settings())
    return RedirectResponse("/config?google=disconnected", status_code=302)


@app.post("/settings/google/cro/disconnect")
def google_cro_disconnect():
    google_oauth.disconnect_cro(get_settings())
    return RedirectResponse("/config?google_cro=disconnected", status_code=302)


# --- Figma OAuth (Connect Figma for file comments) ---------------------------
@app.get("/settings/figma/connect")
def figma_connect():
    if not figma_oauth.oauth_app_configured():
        return RedirectResponse("/config?figma=notconfigured", status_code=302)
    try:
        url = figma_oauth.build_auth_url(state=figma_oauth.STATE)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/config?figma=error&msg={quote(str(e))}", status_code=302
        )
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/figma/callback")
def figma_callback(code: str = "", error: str = "", state: str = ""):
    dest = "/config"
    if error or not code:
        return RedirectResponse(
            f"{dest}?figma=error&msg={quote(error or 'no code')}", status_code=302
        )
    if (state or "").strip() and (state or "").strip() != figma_oauth.STATE:
        return RedirectResponse(
            f"{dest}?figma=error&msg={quote('invalid state')}", status_code=302
        )
    try:
        figma_oauth.exchange_code(code, settings=get_settings())
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"{dest}?figma=error&msg={quote(f'{type(e).__name__}: {e}')}",
            status_code=302,
        )
    return RedirectResponse(f"{dest}?figma=connected", status_code=302)


@app.post("/settings/figma/disconnect")
def figma_disconnect():
    figma_oauth.disconnect(get_settings())
    return RedirectResponse("/config?figma=disconnected", status_code=302)


@app.post("/settings/figma/pat")
def figma_save_pat(figma_access_token: str = Form("")):
    """Save a Figma personal access token from Config (Postgres, not .env)."""
    from designops.adapters import figma as figma_api

    tok = (figma_access_token or "").strip()
    if not tok:
        return RedirectResponse(
            "/config?figma_pat=error&msg=" + quote("paste a token first"),
            status_code=302,
        )
    try:
        figma_api.save_pat(tok)
    except figma_api.FigmaError as e:
        return RedirectResponse(
            f"/config?figma_pat=error&msg={quote(str(e))}", status_code=302
        )
    return RedirectResponse("/config?figma_pat=saved", status_code=302)


@app.post("/settings/figma/pat/clear")
def figma_clear_pat():
    from designops.adapters import figma as figma_api

    figma_api.clear_pat()
    return RedirectResponse("/config?figma_pat=cleared", status_code=302)


@app.post("/settings/figma/oauth-app")
def figma_save_oauth_app(
    figma_client_id: str = Form(""),
    figma_client_secret: str = Form(""),
    figma_redirect_uri: str = Form(""),
):
    """Save Figma OAuth app credentials from Config (Postgres, not .env)."""
    existing = figma_oauth.get_oauth_app()
    cid = (figma_client_id or "").strip() or (existing.get("client_id") or "")
    secret = (figma_client_secret or "").strip() or (existing.get("client_secret") or "")
    redir = (figma_redirect_uri or "").strip()
    try:
        figma_oauth.save_oauth_app(cid, secret, redir)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/config?figma_app=error&msg={quote(str(e))}", status_code=302
        )
    return RedirectResponse("/config?figma_app=saved", status_code=302)


@app.post("/settings/figma/oauth-app/clear")
def figma_clear_oauth_app():
    figma_oauth.clear_oauth_app()
    return RedirectResponse("/config?figma_app=cleared", status_code=302)


@app.get("/api/figma/comments")
def api_figma_comments(
    url: str = "",
    file_key: str = "",
    unresolved_only: bool = False,
    as_md: bool = True,
    limit: int | None = None,
):
    """Fetch Figma file comments (JSON). Prefers OAuth, falls back to PAT.

    Optional ``limit`` returns only the newest N comments in ``comments``
    (useful for Config smoke-tests).
    """
    from designops.adapters import figma as figma_api

    target = (url or file_key or "").strip()
    if not target:
        raise HTTPException(400, "pass url= or file_key=")
    if not figma_api.is_ready():
        raise HTTPException(
            503,
            "Figma not ready — Connect Figma or save a personal access token on Config",
        )
    if limit is not None and limit < 1:
        raise HTTPException(400, "limit must be >= 1")
    try:
        return figma_api.fetch_file_comments(
            target,
            unresolved_only=unresolved_only,
            as_md=as_md,
            limit=limit,
        )
    except figma_api.FigmaError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/settings/figma/probe")
def figma_probe(figma_url: str = Form("")):
    """Config UI smoke-test: fetch comments for a pasted Figma URL."""
    from designops.adapters import figma as figma_api

    target = (figma_url or "").strip()
    if not target:
        return RedirectResponse("/config?figma_probe=error&msg=empty", status_code=302)
    if not figma_api.is_ready():
        return RedirectResponse(
            "/config?figma_probe=error&msg="
            + quote("not ready — Connect Figma or save a personal access token on Config"),
            status_code=302,
        )
    try:
        result = figma_api.fetch_file_comments(target, unresolved_only=False)
    except figma_api.FigmaError as e:
        return RedirectResponse(
            f"/config?figma_probe=error&msg={quote(str(e)[:200])}", status_code=302
        )
    q = (
        f"figma_probe=ok&file_key={quote(result['file_key'])}"
        f"&total={result['total']}&unresolved={result['unresolved_roots']}"
        f"&threads={result['thread_count']}&mode={quote(result['auth_mode'] or '')}"
    )
    return RedirectResponse(f"/config?{q}", status_code=302)


@app.get("/runs/{run_id}/digest", response_class=HTMLResponse)
def run_digest(run_id: str, db: Session = Depends(get_db)):
    art = (
        db.query(Artifact)
        .filter_by(run_id=run_id, kind="html")
        .order_by(Artifact.id.desc())
        .first()
    )
    if not art:
        raise HTTPException(404, "no rendered digest for this run")
    return Response(content=art.content, media_type="text/html")


@app.get("/runs/{run_id}/download")
def download_run_artifacts(run_id: str, db: Session = Depends(get_db)):
    """Zip HTML + JSON artifacts so local vs prod Figma/report diffs are easy to compare."""
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404)
    if run.status == "running":
        raise HTTPException(409, "run still generating")
    arts = (
        db.query(Artifact)
        .filter(Artifact.run_id == run_id, Artifact.kind.in_(("html", "json")))
        .order_by(Artifact.kind.asc(), Artifact.id.desc())
        .all()
    )
    by_kind: dict[str, Artifact] = {}
    for art in arts:
        by_kind.setdefault(art.kind, art)
    if not by_kind:
        raise HTTPException(404, "no artifacts for this run")

    pipeline = db.get(Pipeline, run.pipeline_id)
    pipe_key = (pipeline.key if pipeline else "run") or "run"
    day = run.report_date.isoformat() if run.report_date else "unknown"
    short = str(run.id).replace("-", "")[:8]
    base = f"{pipe_key}-{day}-{short}"

    meta = {
        "run_id": str(run.id),
        "pipeline": pipe_key,
        "report_date": day,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "skill_version": run.skill_version,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost_usd": float(run.cost_usd or 0),
        "counts": run.counts or {},
        "error": run.error,
        "note": run.note,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base}-meta.json", json.dumps(meta, indent=2, default=str))
        if "html" in by_kind and by_kind["html"].content is not None:
            zf.writestr(f"{base}.html", by_kind["html"].content)
        if "json" in by_kind and by_kind["json"].content is not None:
            raw = by_kind["json"].content
            try:
                pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
            except (TypeError, ValueError, json.JSONDecodeError):
                pretty = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            zf.writestr(f"{base}.json", pretty)
    data = buf.getvalue()
    headers = {
        "Content-Disposition": f'attachment; filename="{base}.zip"',
        "Content-Length": str(len(data)),
    }
    return Response(content=data, media_type="application/zip", headers=headers)


@app.post("/runs/{run_id}/validate")
def validate_run(
    run_id: str,
    validation_note: str = Form(""),
    db: Session = Depends(get_db),
):
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404)
    run.validated_by = "liana.staskevica@scandiweb.com"
    run.validated_at = datetime.now()
    run.validation_note = validation_note or None
    db.add(run)
    return RedirectResponse(f"/runs/{run_id}", status_code=302)


@app.get("/compare", response_class=HTMLResponse)
def compare(a: str, b: str, request: Request, db: Session = Depends(get_db)):
    ra, rb = db.get(PipelineRun, a), db.get(PipelineRun, b)
    if not ra or not rb:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "compare.html", {"request": request, "a": ra, "b": rb, "nav": "runs"}
    )


# --- Accounts (§11) + on-demand export explorer -------------------------------
@app.get("/accounts", response_class=HTMLResponse)
def accounts_screen(request: Request, db: Session = Depends(get_db)):
    accounts = (
        db.query(Account).filter_by(is_active=True).order_by(Account.name).all()
    )
    total = db.query(Account).count()
    enabled = db.query(Account).filter_by(digest_enabled=True).count()
    default_to = _prev_working_day(date.today())
    default_from = default_to - timedelta(days=6)
    return templates.TemplateResponse(
        "accounts.html",
        {
            "request": request, "accounts": accounts, "nav": "accounts",
            "total": total, "enabled": enabled, "active": len(accounts),
            "default_from": default_from.isoformat(), "default_to": default_to.isoformat(),
            "fairwind_configured": get_settings().fairwind_configured,
            "data_types": EXPLORER_DATA_TYPES,
        },
    )


@app.post("/accounts/{account_id}/enable")
def account_enable(
    account_id: str,
    enable: str = Form(...),
    next: str = Form("/weekly-health"),
    db: Session = Depends(get_db),
):
    """Toggle whether this account is included in the daily report (§11 allowlist)."""
    a = db.query(Account).filter_by(fairwind_account_id=account_id).one_or_none()
    if not a:
        raise HTTPException(404, "unknown account")
    on = enable == "on"
    a.digest_enabled = on
    a.enabled_by = get_settings().setup_owner_email if on else None
    a.enabled_at = datetime.now() if on else None
    if on:
        a.notes = "Enabled from Accounts UI."
        # keep the registry in step so this account's work isn't flagged "untracked"
        from designops.core.projects import ensure_project_for_account

        ensure_project_for_account(db, a)
    db.add(a)
    nxt = safe_next_url(next, fallback="/weekly-health")
    return RedirectResponse(nxt, status_code=302)


@app.post("/accounts/{account_id}/export", response_class=HTMLResponse)
def account_export(
    account_id: str,
    request: Request,
    date_from: str = Form(...),
    date_to: str = Form(...),
    db: Session = Depends(get_db),
):
    account = (
        db.query(Account).filter_by(fairwind_account_id=account_id).one_or_none()
    )
    if not account:
        raise HTTPException(404, "unknown account")
    ctx = {"request": request, "nav": "accounts", "account": account,
           "date_from": date_from, "date_to": date_to, "data_types": EXPLORER_DATA_TYPES}
    try:
        df, dt = date.fromisoformat(date_from), date.fromisoformat(date_to)
        if df > dt:
            raise ValueError("date_from is after date_to")
        client = FairwindClient(get_settings())
        result = client.export_account(
            account.fairwind_account_id, df, dt, data_types=EXPLORER_DATA_TYPES
        )
        ctx["result"] = result
    except (FairwindError, ValueError) as e:
        ctx["error"] = str(e)
    return templates.TemplateResponse("account_export_result.html", ctx)


def _account_export_zip_path(account_id: str, date_from: date, date_to: date) -> Path:
    store = Path(get_settings().corpus_store_dir).resolve()
    root = (store / account_id / f"{date_from.isoformat()}_{date_to.isoformat()}").resolve()
    zip_path = (root / "export.zip").resolve()
    try:
        zip_path.relative_to(store)
    except ValueError as e:
        raise HTTPException(400, "invalid export path") from e
    if zip_path.name != "export.zip":
        raise HTTPException(400, "invalid export path")
    return zip_path


@app.get("/accounts/{account_id}/export/download")
def account_export_download(
    account_id: str,
    date_from: str,
    date_to: str,
    db: Session = Depends(get_db),
):
    account = (
        db.query(Account).filter_by(fairwind_account_id=account_id).one_or_none()
    )
    if not account:
        raise HTTPException(404, "unknown account")
    try:
        df, dt = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError as e:
        raise HTTPException(400, "invalid dates") from e
    if df > dt:
        raise HTTPException(400, "date_from is after date_to")
    zip_path = _account_export_zip_path(account_id, df, dt)
    if not zip_path.is_file():
        raise HTTPException(404, "no export zip for that range — run the export first")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (account.name or account_id).strip())[:60].strip("-")
    filename = f"{slug or account_id}-{df.isoformat()}_{dt.isoformat()}.zip"
    return FileResponse(zip_path, media_type="application/zip", filename=filename)


# --- Config: roster (§0 screen 5) --------------------------------------------
def _parse_email_list(raw: str) -> list[str]:
    parts = re.split(r"[\s,;]+", (raw or "").strip())
    return [p.lower() for p in parts if p and "@" in p]


def _parse_optional_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return date.fromisoformat(raw)


def _parse_optional_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return float(raw)


def _apply_person_form(
    person: Person,
    *,
    full_name: str,
    emails: str,
    jira_account_id: str,
    role: str,
    status: str,
    leave_from: str,
    leave_until: str,
    is_dedicated: str,
    dedicated_weekly_hours: str,
) -> None:
    name = (full_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="full_name is required")
    email_list = _parse_email_list(emails)
    if not email_list:
        raise HTTPException(status_code=400, detail="at least one email is required")
    st = (status or "active").strip()
    if st not in {s.value for s in PersonStatus}:
        st = PersonStatus.ACTIVE.value
    prev_emails = [e.lower() for e in (person.emails or [])]
    person.full_name = name
    person.emails = email_list
    person.jira_account_id = (jira_account_id or "").strip() or None
    person.role = (role or "").strip() or None
    person.status = st
    person.leave_from = _parse_optional_date(leave_from)
    person.leave_until = _parse_optional_date(leave_until)
    person.is_dedicated = is_dedicated == "on"
    try:
        person.dedicated_weekly_hours = _parse_optional_float(dedicated_weekly_hours)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="dedicated hours must be a number") from e
    if person.dedicated_weekly_hours is not None and person.dedicated_weekly_hours > 0:
        person.is_dedicated = True
    if not person.is_dedicated:
        person.dedicated_weekly_hours = None
    # Email change invalidates a prior verification — re-check via Verify identity.
    if prev_emails and set(prev_emails) != set(email_list):
        person.jira_verified = False
        person.fairwind_verified = False
        person.identity_verified = False


@app.get("/people", response_class=HTMLResponse)
def people_screen(request: Request, db: Session = Depends(get_db)):
    return _config_page(request, db, panel="people")


@app.get("/config", response_class=HTMLResponse)
def config_screen(request: Request, db: Session = Depends(get_db)):
    return _config_page(request, db, panel="connections")


def _config_page(request: Request, db: Session, *, panel: str):
    people = db.query(Person).order_by(Person.status, Person.full_name).all()
    today = date.today()
    for p in people:
        # List / stats use today; edit form still binds to stored status + dates.
        p.display_status = effective_status(
            p.status, p.leave_until, today, leave_from=p.leave_from
        )
        p.leave_upcoming = (
            p.status == PersonStatus.ON_LEAVE
            and p.display_status == PersonStatus.ACTIVE
            and bool(p.leave_from and p.leave_from > today)
        )
    people.sort(key=lambda p: (p.display_status, (p.full_name or "").lower()))
    projects = db.query(Project).order_by(Project.canonical_name).all()
    s = get_settings()
    cro_msgs: list[dict] = []
    cro_err: str | None = None
    if panel == "connections" and google_oauth.is_cro_connected(s):
        try:
            from designops.adapters.gmail import list_cro_messages

            cro_msgs = list_cro_messages(max_results=10, settings=s)
        except Exception as e:  # noqa: BLE001 — show in UI, don't break Config
            cro_err = f"{type(e).__name__}: {e}"
    google_flash = request.query_params.get("google")
    google_cro_flash = request.query_params.get("google_cro")
    figma_flash = request.query_params.get("figma")
    figma_probe = request.query_params.get("figma_probe")
    figma_pat_flash = request.query_params.get("figma_pat")
    figma_app_flash = request.query_params.get("figma_app")
    msg = request.query_params.get("msg")
    from designops.adapters import figma as figma_api

    app_hint = figma_oauth.oauth_app_hint()
    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "people": people,
            "projects": projects,
            "nav": "people" if panel == "people" else "config",
            "panel": panel,
            "flash": request.query_params.get("flash"),
            "google_flash": google_flash,
            "google_cro_flash": google_cro_flash,
            "figma_flash": figma_flash,
            "figma_probe": figma_probe,
            "figma_pat_flash": figma_pat_flash,
            "figma_app_flash": figma_app_flash,
            "google_msg": msg if google_flash else None,
            "google_cro_msg": msg if google_cro_flash else None,
            "figma_msg": msg if figma_flash else None,
            "figma_probe_msg": msg if figma_probe else None,
            "figma_pat_msg": msg if figma_pat_flash else None,
            "figma_app_msg": msg if figma_app_flash else None,
            "figma_probe_file_key": request.query_params.get("file_key"),
            "figma_probe_total": request.query_params.get("total"),
            "figma_probe_unresolved": request.query_params.get("unresolved"),
            "figma_probe_threads": request.query_params.get("threads"),
            "figma_probe_mode": request.query_params.get("mode"),
            "verify_msg": (
                msg
                if not google_flash
                and not google_cro_flash
                and not figma_flash
                and not figma_probe
                and not figma_pat_flash
                and not figma_app_flash
                else None
            ),
            "verify_person": request.query_params.get("person"),
            "statuses": [s.value for s in PersonStatus],
            "google_configured": s.google_oauth_configured,
            "google_connected": google_oauth.is_connected(s),
            "google_email": google_oauth.connected_email(s),
            "smtp_ready": s.smtp_configured,
            "owner_email": s.setup_owner_email,
            "cro_mailbox": s.cro_mailbox_email,
            "cro_connected": google_oauth.is_cro_connected(s),
            "cro_email": google_oauth.connected_cro_email(s),
            "cro_messages": cro_msgs,
            "cro_error": cro_err,
            "figma_oauth_configured": figma_oauth.oauth_app_configured(),
            "figma_client_id_hint": app_hint.get("client_id_hint"),
            "figma_client_secret_hint": app_hint.get("client_secret_hint"),
            "figma_pat_configured": figma_api.pat_configured(),
            "figma_pat_hint": figma_api.pat_hint(),
            "figma_connected": figma_oauth.is_connected(s),
            "figma_label": figma_oauth.connected_label(s),
            "figma_auth_mode": figma_api.auth_mode(s),
            "figma_ready": figma_api.is_ready(s),
            "figma_redirect_uri": app_hint.get("redirect_uri"),
        },
    )


@app.post("/config/verify-identity")
async def config_verify_identity(request: Request):
    """Lookup-only identity check (no DB write). Used by Add designer before save."""
    from designops.adapters.identity_check import check_identity

    emails: list[str] = []
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        raw = body.get("emails")
        if isinstance(raw, str) and raw.strip():
            emails = _parse_email_list(raw)
        elif isinstance(raw, list) and raw:
            emails = [str(e).strip().lower() for e in raw if e and "@" in str(e)]
    if not emails:
        raise HTTPException(status_code=400, detail="email is required")

    result = check_identity(emails)
    return JSONResponse(
        {
            "ok": True,
            "verified": result.verified,
            "summary": result.summary,
            "jira_ok": result.jira_ok,
            "jira_account_id": result.jira_account_id,
            "jira_display_name": result.jira_display_name,
            "jira_error": result.jira_error,
            "fairwind_ok": result.fairwind_ok,
            "fairwind_detail": result.fairwind_detail,
            "email": result.email,
        }
    )


@app.post("/config/people")
def config_people_create(
    full_name: str = Form(""),
    emails: str = Form(""),
    jira_account_id: str = Form(""),
    role: str = Form(""),
    status: str = Form("active"),
    leave_from: str = Form(""),
    leave_until: str = Form(""),
    is_dedicated: str = Form("off"),
    dedicated_weekly_hours: str = Form(""),
    jira_verified: str = Form("off"),
    fairwind_verified: str = Form("off"),
    db: Session = Depends(get_db),
):
    jira_ok = jira_verified == "on"
    fw_ok = fairwind_verified == "on"
    person = Person(
        display_aliases=[],
        jira_verified=jira_ok,
        fairwind_verified=fw_ok,
        identity_verified=jira_ok and fw_ok,
    )
    _apply_person_form(
        person,
        full_name=full_name,
        emails=emails,
        jira_account_id=jira_account_id,
        role=role,
        status=status,
        leave_from=leave_from,
        leave_until=leave_until,
        is_dedicated=is_dedicated,
        dedicated_weekly_hours=dedicated_weekly_hours,
    )
    # Re-apply verify flags after _apply_person_form (email change logic shouldn't clear on create)
    person.jira_verified = jira_ok
    person.fairwind_verified = fw_ok
    person.identity_verified = jira_ok and fw_ok
    db.add(person)
    db.commit()
    return RedirectResponse("/people?flash=added", status_code=302)


@app.post("/config/people/{person_id}")
def config_people_update(
    person_id: str,
    full_name: str = Form(""),
    emails: str = Form(""),
    jira_account_id: str = Form(""),
    role: str = Form(""),
    status: str = Form("active"),
    leave_from: str = Form(""),
    leave_until: str = Form(""),
    is_dedicated: str = Form("off"),
    dedicated_weekly_hours: str = Form(""),
    db: Session = Depends(get_db),
):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    _apply_person_form(
        person,
        full_name=full_name,
        emails=emails,
        jira_account_id=jira_account_id,
        role=role,
        status=status,
        leave_from=leave_from,
        leave_until=leave_until,
        is_dedicated=is_dedicated,
        dedicated_weekly_hours=dedicated_weekly_hours,
    )
    db.add(person)
    db.commit()
    return RedirectResponse("/people?flash=saved", status_code=302)


@app.post("/config/people/{person_id}/verify")
async def config_people_verify(
    person_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    from designops.adapters.identity_check import check_identity

    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")

    emails = list(person.emails or [])
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        raw = body.get("emails")
        if isinstance(raw, str) and raw.strip():
            emails = _parse_email_list(raw)
        elif isinstance(raw, list) and raw:
            emails = [str(e).strip().lower() for e in raw if e and "@" in str(e)]

    result = check_identity(emails)
    if result.jira_ok and result.jira_account_id:
        person.jira_account_id = result.jira_account_id
    # Keep DB emails in sync when verify used the form's current value
    if emails and set(emails) != set(e.lower() for e in (person.emails or [])):
        person.emails = emails
    person.jira_verified = result.jira_ok
    person.fairwind_verified = result.fairwind_ok
    person.identity_verified = result.verified
    db.add(person)
    db.commit()

    return JSONResponse(
        {
            "ok": True,
            "verified": result.verified,
            "summary": result.summary,
            "jira_ok": result.jira_ok,
            "jira_account_id": result.jira_account_id,
            "jira_display_name": result.jira_display_name,
            "jira_error": result.jira_error,
            "fairwind_ok": result.fairwind_ok,
            "fairwind_detail": result.fairwind_detail,
            "email": result.email,
        }
    )


@app.post("/config/people/{person_id}/mark-out")
def config_people_mark_out(person_id: str, db: Session = Depends(get_db)):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    person.status = PersonStatus.OUT.value
    db.add(person)
    db.commit()
    return RedirectResponse("/people?flash=removed", status_code=302)


@app.post("/config/people/{person_id}/delete")
def config_people_delete(person_id: str, db: Session = Depends(get_db)):
    """Permanently remove a roster person. Historical run docs keep null person_id."""
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    name = person.full_name
    db.query(RunDocument).filter(RunDocument.person_id == person.id).update(
        {RunDocument.person_id: None}, synchronize_session=False
    )
    db.query(Flag).filter(Flag.person_id == person.id).update(
        {Flag.person_id: None}, synchronize_session=False
    )
    db.delete(person)
    db.commit()
    return JSONResponse({"ok": True, "deleted": True, "name": name})
