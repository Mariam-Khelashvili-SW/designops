"""In-process scheduler (APScheduler) for all enabled pipelines.

Each Pipeline row is the source of truth: `schedule_cron`, `timezone`, `recipients`,
`send_mode`, `enabled`. Editing in the UI calls `reschedule()`. Nothing fires while
`enabled` is false.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

_TZ = ZoneInfo("Europe/Riga")
# Explicit timezone on the scheduler — without it, cron jobs can show a
# computed next time in the UI while never waking up to fire.
_scheduler = BackgroundScheduler(timezone=_TZ)
_log = logging.getLogger("designops.scheduler")

# Backoff between scheduled retries when generate/send fails (seconds).
_RETRY_BASE_SEC = 45
_RETRY_CAP_SEC = 300  # 5 min


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()
    _log_jobs("start_scheduler")


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

    known_ids = {
        j.id
        for j in _scheduler.get_jobs()
        if not str(j.id).endswith(":once")  # keep one-shot test jobs
    }
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
        except (ValueError, KeyError) as e:
            print(f"  ⚠ cron invalid for {job_id}: {spec['cron']!r} ({e})", flush=True)
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
        job = _scheduler.get_job(job_id)
        nrt = getattr(job, "next_run_time", None) if job else None
        print(
            f"  ⏰ scheduled {job_id} cron={spec['cron']!r} next={nrt} "
            f"running={_scheduler.running}",
            flush=True,
        )
        if job is not None and nrt is None and _scheduler.running:
            print(
                f"  ⚠ {job_id} has no next_run_time — job may never fire",
                flush=True,
            )
    for jid in known_ids - wanted:
        if _scheduler.get_job(jid):
            _scheduler.remove_job(jid)


def schedule_once(pipeline_key: str, *, delay_seconds: int = 90) -> datetime:
    """Fire a pipeline once after delay_seconds (DateTrigger — reliable for tests)."""
    when = datetime.now(_TZ) + timedelta(seconds=delay_seconds)
    job_id = f"{pipeline_key}:once"
    _scheduler.add_job(
        _run_scheduled,
        DateTrigger(run_date=when, timezone=_TZ),
        id=job_id,
        replace_existing=True,
        kwargs={"pipeline_key": pipeline_key},
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    job = _scheduler.get_job(job_id)
    print(
        f"  ⏰ one-shot {pipeline_key} at {when} next={getattr(job, 'next_run_time', None)}",
        flush=True,
    )
    return when


def next_run_time(pipeline_key: str = "daily-digest") -> datetime | None:
    """Real next fire from the live scheduler — not a trigger estimate."""
    if not _scheduler.running:
        return None
    job = _scheduler.get_job(pipeline_key)
    once = _scheduler.get_job(f"{pipeline_key}:once")
    candidates = []
    for j in (job, once):
        if j is None:
            continue
        nrt = getattr(j, "next_run_time", None)
        if nrt is not None:
            candidates.append(nrt)
    return min(candidates) if candidates else None


def _log_jobs(label: str) -> None:
    jobs = _scheduler.get_jobs()
    print(
        f"  ⏰ scheduler[{label}] running={_scheduler.running} jobs={len(jobs)}",
        flush=True,
    )
    for j in jobs:
        print(
            f"     - {j.id} next={getattr(j, 'next_run_time', None)}",
            flush=True,
        )


def _prev_working_day(today) -> datetime.date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


def _delivery_succeeded(send_mode: str, delivery_status: str | None) -> bool:
    """Whether the run's email step met the pipeline's send_mode."""
    mode = (send_mode or "none").lower()
    status = (delivery_status or "not_sent").lower()
    if mode in ("", "none"):
        return True
    if mode == "send":
        return status == "sent"
    if mode == "self":
        return status == "self"
    if mode == "draft":
        return status == "draft"
    return False


def _record_scheduler_failure(pipeline_key: str, attempt: int, err: BaseException) -> str | None:
    """Persist a visible failed run when the attempt blew up before/without a run row."""
    from designops.core.db import session_scope
    from designops.core.enums import RunStatus
    from designops.core.models import Pipeline, PipelineRun

    today = datetime.now(_TZ).date()
    try:
        with session_scope() as s:
            p = s.query(Pipeline).filter_by(key=pipeline_key).one_or_none()
            if not p:
                return None
            run = PipelineRun(
                pipeline_id=p.id,
                report_date=today,
                started_at=datetime.now(_TZ),
                finished_at=datetime.now(_TZ),
                status=RunStatus.FAILED,
                error=str(err)[:2000],
                note=f"scheduled attempt {attempt} failed",
            )
            s.add(run)
            s.flush()
            return str(run.id)
    except Exception as e:  # noqa: BLE001
        _log.exception("could not record failed run for %s: %s", pipeline_key, e)
        return None


def _attempt_scheduled(pipeline_key: str, attempt: int) -> tuple[bool, str | None, str]:
    """One generate(+deliver) attempt. Commits the run so failures stay visible.

    Returns (ok, run_id, detail).
    """
    from designops.core.db import session_scope
    from designops.core.enums import RunStatus
    from designops.core.models import Artifact, Pipeline

    today = datetime.now(_TZ).date()

    try:
        with session_scope() as s:
            p = s.query(Pipeline).filter_by(key=pipeline_key).one_or_none()
            if not p or not p.enabled:
                return True, None, "pipeline disabled — skip"

            if pipeline_key == "daily-digest":
                from designops.pipelines.daily_digest import run_daily_digest

                run = run_daily_digest(s, _prev_working_day(today), reuse_ingest=True)
            elif pipeline_key == "weekly-backlog":
                from designops.pipelines.weekly_availability import resolve_week_monday
                from designops.pipelines.weekly_backlog import run_weekly_backlog

                run = run_weekly_backlog(
                    s, resolve_week_monday(today), reuse_ingest=True
                )
            elif pipeline_key == "weekly-health":
                from designops.pipelines.weekly_health import run_weekly_health

                run = run_weekly_health(s, reuse_ingest=True)
            else:
                return True, None, f"unknown pipeline {pipeline_key}"

            s.flush()
            run_id = str(run.id)
            if run.status == RunStatus.FAILED:
                return False, run_id, run.error or "run status=failed"

            art = (
                s.query(Artifact)
                .filter_by(run_id=run.id, kind="html")
                .order_by(Artifact.id.desc())
                .first()
            )
            delivery = art.delivery_status if art else None
            if (p.send_mode or "").lower() == "send" and delivery == "blocked_go_live":
                return True, run_id, "blocked_go_live — not retrying"

            if _delivery_succeeded(p.send_mode, delivery):
                return True, run_id, f"ok delivery={delivery or 'n/a'}"

            return (
                False,
                run_id,
                f"delivery={delivery or 'missing'} (want send_mode={p.send_mode})",
            )
    except Exception as e:  # noqa: BLE001 — keep failed attempt visible, then retry
        _log.exception("%s scheduled attempt %s crashed", pipeline_key, attempt)
        run_id = _record_scheduler_failure(pipeline_key, attempt, e)
        return False, run_id, f"{type(e).__name__}: {e}"


def _run_scheduled(pipeline_key: str = "daily-digest") -> None:
    """Scheduled job — run the pipeline (it owns generate + deliver).

    On generate/send failure, the failed run stays in the DB and we retry with
    backoff until delivery succeeds (or go_live blocks permanently).
    Do not call send_digest here: runners already email via deliver().
    """
    print(f"  ▶ scheduled fire {pipeline_key}", flush=True)
    attempt = 0
    while True:
        attempt += 1
        ok, run_id, detail = _attempt_scheduled(pipeline_key, attempt)
        if ok:
            msg = (
                f"{pipeline_key} scheduled ok on attempt {attempt} "
                f"run={run_id} ({detail})"
            )
            _log.info(msg)
            print(f"  ✓ {msg}", flush=True)
            return
        delay = min(_RETRY_CAP_SEC, _RETRY_BASE_SEC * attempt)
        msg = (
            f"{pipeline_key} scheduled attempt {attempt} failed "
            f"run={run_id} ({detail}) — retry in {delay}s"
        )
        _log.warning(msg)
        print(f"  ⚠ {msg}", flush=True)
        time.sleep(delay)
