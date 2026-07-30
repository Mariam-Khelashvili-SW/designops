"""In-process scheduler (APScheduler) for all enabled pipelines.

Each Pipeline row is the source of truth: `schedule_cron`, `timezone`, `recipients`,
`send_mode`, `enabled`. Editing in the UI calls `reschedule()`. Nothing fires while
`enabled` is false.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = BackgroundScheduler()


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()


def reschedule() -> None:
    """(Re)install cron jobs from every enabled pipeline row."""
    from designops.core.db import session_scope
    from designops.core.models import Pipeline

    with session_scope() as s:
        rows = s.query(Pipeline).all()
        specs = [
            {
                "key": p.key,
                "cron": p.schedule_cron,
                "tz": p.timezone or "Europe/Riga",
                "enabled": bool(p.enabled and p.schedule_cron),
            }
            for p in rows
        ]

    known_ids = {j.id for j in _scheduler.get_jobs()}
    wanted: set[str] = set()
    for spec in specs:
        job_id = spec["key"]
        if not spec["enabled"]:
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            continue
        try:
            trigger = CronTrigger.from_crontab(
                spec["cron"], timezone=ZoneInfo(spec["tz"])
            )
        except (ValueError, KeyError):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            continue
        wanted.add(job_id)
        _scheduler.add_job(
            _run_scheduled,
            trigger,
            id=job_id,
            replace_existing=True,
            kwargs={"pipeline_key": job_id},
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    for jid in known_ids - wanted:
        # Drop stale jobs for pipelines that were deleted or disabled
        if _scheduler.get_job(jid):
            _scheduler.remove_job(jid)


def next_run_time(pipeline_key: str = "daily-digest") -> datetime | None:
    job = _scheduler.get_job(pipeline_key)
    if not job:
        return None
    nrt = getattr(job, "next_run_time", None)
    if nrt is not None:
        return nrt
    try:
        tz = getattr(job.trigger, "timezone", None)
        return job.trigger.get_next_fire_time(None, datetime.now(tz))
    except Exception:  # noqa: BLE001
        return None


def _prev_working_day(today) -> datetime.date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


def _run_scheduled(pipeline_key: str = "daily-digest") -> None:
    """Scheduled job dispatcher — generate then optionally email."""
    from designops.adapters.delivery import send_digest
    from designops.core.db import session_scope
    from designops.core.models import Artifact, Pipeline

    tz = ZoneInfo("Europe/Riga")
    today = datetime.now(tz).date()

    with session_scope() as s:
        p = s.query(Pipeline).filter_by(key=pipeline_key).one_or_none()
        if not p or not p.enabled:
            return

        if pipeline_key == "daily-digest":
            from designops.pipelines.daily_digest import run_daily_digest

            report_date = _prev_working_day(today)
            run = run_daily_digest(s, report_date, reuse_ingest=True)
            subject = f"Design Daily Digest — {report_date.isoformat()}"
        elif pipeline_key == "weekly-backlog":
            from designops.pipelines.weekly_availability import resolve_week_monday
            from designops.pipelines.weekly_backlog import run_weekly_backlog

            # Monday job: week_of = today if Monday; weekends → next Monday
            week_monday = resolve_week_monday(today)
            run = run_weekly_backlog(s, week_monday, reuse_ingest=True)
            subject = f"Design Weekly Backlog — week of {week_monday.isoformat()}"
        elif pipeline_key == "weekly-health":
            from designops.pipelines.weekly_health import run_weekly_health

            # Snapshot: always reports the latest state as of the run day.
            run = run_weekly_health(s, reuse_ingest=True)
            subject = f"Design Weekly Health & Budget — {today.isoformat()}"
        else:
            return

        s.flush()
        if run.status == "failed" or p.send_mode != "send" or not p.recipients:
            return
        art = (
            s.query(Artifact)
            .filter_by(run_id=run.id, kind="html")
            .order_by(Artifact.id.desc())
            .first()
        )
        if not art:
            return
        result = send_digest(
            list(p.recipients),
            subject,
            art.content,
            status_label="sent",
        )
        art.delivery_status = result.status
        art.delivered_at = datetime.now() if result.status not in ("failed",) else None
        art.message_id = result.message_id
        s.add(art)
