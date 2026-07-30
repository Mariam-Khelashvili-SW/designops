"""FastAPI read UI (§0, §12.3). Server-rendered Jinja, no SPA, no build step.

Screens: Pipelines · Run log · Artifact viewer (3 panes) · Accounts · Config.
On-demand run: POST /pipelines/daily-digest/run (§12.1). Delivery stays gated by
go_live in the delivery adapter regardless of anything toggled here (§12.4).
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from designops.adapters import google_oauth
from designops.adapters.delivery import send_digest
from designops.adapters.fairwind import FairwindClient, FairwindError
from designops.core.config import get_settings
from designops.core.db import get_db
from designops.core.enums import SendMode
from designops.core.models import (
    Account,
    Artifact,
    Flag,
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


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/daily-report", status_code=302)


# --- Daily report -------------------------------------------------------------
def _prev_working_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _cron_to_fields(cron: str | None) -> tuple[str, str]:
    """cron 'M H * * D' -> ('HH:MM', 'weekdays'|'all') for the schedule form."""
    try:
        m, h, _, _, dow = (cron or "0 12 * * 1-5").split()[:5]
        return f"{int(h):02d}:{int(m):02d}", ("all" if dow == "*" else "weekdays")
    except (ValueError, TypeError):
        return "12:00", "weekdays"


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
        .limit(8)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    # schedule config from the pipeline row (cron -> HH:MM + weekdays/every-day)
    sched_time, sched_days = _cron_to_fields(pipeline.schedule_cron if pipeline else None)
    from designops.api.scheduler import next_run_time
    nrt = next_run_time()
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
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
            # delivery status
            "google_configured": s.google_oauth_configured,
            "google_connected": google_oauth.is_connected(s),
            "google_email": google_oauth.connected_email(s),
            "smtp_ready": s.smtp_configured,
            "owner_email": s.setup_owner_email,
            "google_flash": request.query_params.get("google"),
            "google_msg": request.query_params.get("msg"),
        },
    )


def _bg_execute_run(run_id: str, reuse_ingest: bool) -> None:
    """Background worker: run the heavy stages in a fresh session, updating the run row.
    execute_run sets a terminal status even on failure, so the UI never sticks on running."""
    from designops.core.db import session_scope
    from designops.pipelines.daily_digest import execute_run as execute_daily
    from designops.pipelines.weekly_backlog import execute_run as execute_weekly
    from designops.pipelines.weekly_health import execute_run as execute_weekly_health

    with session_scope() as s:
        run = s.get(PipelineRun, run_id)
        if run is None:
            return
        pipe = s.get(Pipeline, run.pipeline_id)
        if pipe and pipe.key == WEEKLY_KEY:
            execute_weekly(s, run, reuse_ingest=reuse_ingest)
        elif pipe and pipe.key == WEEKLY_HEALTH_KEY:
            execute_weekly_health(s, run, reuse_ingest=reuse_ingest)
        else:
            execute_daily(s, run, reuse_ingest=reuse_ingest)


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


# --- Pipelines ----------------------------------------------------------------
@app.get("/pipelines", response_class=HTMLResponse)
def pipelines(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Pipeline).order_by(Pipeline.key).all()
    data = []
    for p in rows:
        last = (
            db.query(PipelineRun)
            .filter_by(pipeline_id=p.id)
            .order_by(PipelineRun.started_at.desc())
            .first()
        )
        data.append({"p": p, "last": last})
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
        .limit(8)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    sched_time, _ = _cron_to_fields(pipeline.schedule_cron if pipeline else "0 11 * * 1")
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
            "sched_recipients": ", ".join(pipeline.recipients) if pipeline else "",
            "sched_send": (pipeline.send_mode if pipeline else "none"),
            "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
            "normal_hours": s.normal_week_hours,
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
    p.schedule_cron = f"{m} {h} * * 1"  # Mondays only
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
        .limit(8)
        .all()
        if pipeline
        else []
    )
    s = get_settings()
    sched_time, _ = _cron_to_fields(pipeline.schedule_cron if pipeline else "0 11 * * 1")
    from designops.api.scheduler import next_run_time

    nrt = next_run_time(WEEKLY_HEALTH_KEY)
    tracked = (
        db.query(Project)
        .filter_by(track_weekly_health=True, active=True)
        .order_by(Project.canonical_name)
        .all()
    )
    return templates.TemplateResponse(
        "weekly_health.html",
        {
            "request": request,
            "nav": "weekly_health",
            "pipeline": pipeline,
            "recent": recent,
            "tracked": tracked,
            "today_label": date.today().strftime("%a %-d %b %Y"),
            "anthropic_ready": s.anthropic_configured,
            "jira_ready": s.jira_configured,
            "sched_enabled": bool(pipeline and pipeline.enabled),
            "sched_time": sched_time,
            "sched_recipients": ", ".join(pipeline.recipients) if pipeline else "",
            "sched_send": (pipeline.send_mode if pipeline else "none"),
            "sched_next": nrt.strftime("%a %d %b %H:%M %Z") if nrt else None,
            "can_send": google_oauth.is_connected() or s.smtp_configured,
            "flash": request.query_params.get("flash"),
        },
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
    sched_time: str = Form("11:00"),
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
        h, m = 11, 0
    p.schedule_cron = f"{m} {h} * * 1"
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
    p.schedule_cron = f"{m} {h} * * {'*' if days == 'all' else '1-5'}"
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


# --- Google OAuth (Connect Gmail for sending) --------------------------------
@app.get("/settings/google/connect")
def google_connect():
    s = get_settings()
    if not s.google_oauth_configured:
        return RedirectResponse("/daily-report?google=notconfigured", status_code=302)
    url = google_oauth.build_auth_url(state="designops", settings=s)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/google/callback")
def google_callback(code: str = "", error: str = ""):
    from urllib.parse import quote

    if error or not code:
        return RedirectResponse(
            f"/daily-report?google=error&msg={quote(error or 'no code')}", status_code=302
        )
    try:
        google_oauth.exchange_code(code, settings=get_settings())
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/daily-report?google=error&msg={quote(f'{type(e).__name__}: {e}')}",
            status_code=302,
        )
    return RedirectResponse("/daily-report?google=connected", status_code=302)


@app.post("/settings/google/disconnect")
def google_disconnect():
    google_oauth.disconnect(get_settings())
    return RedirectResponse("/daily-report?google=disconnected", status_code=302)


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
    db.add(a)
    if on:
        # keep the registry in step so this account's work isn't flagged "untracked"
        from designops.core.projects import ensure_project_for_account

        ensure_project_for_account(db, a)
    return RedirectResponse("/accounts", status_code=302)


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


# --- Config: roster (§0 screen 5) --------------------------------------------
@app.get("/config", response_class=HTMLResponse)
def config_screen(request: Request, db: Session = Depends(get_db)):
    people = db.query(Person).order_by(Person.status, Person.full_name).all()
    projects = db.query(Project).order_by(Project.canonical_name).all()
    return templates.TemplateResponse(
        "config.html",
        {"request": request, "people": people, "projects": projects, "nav": "config"},
    )
