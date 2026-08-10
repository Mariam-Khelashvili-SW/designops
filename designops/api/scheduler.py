"""In-process scheduler (APScheduler) for all enabled pipelines.

Each Pipeline row is the source of truth: `schedule_cron`, `timezone`, `recipients`,
`send_mode`, `enabled`. Editing in the UI calls `reschedule()`. Nothing fires while
`enabled` is false.

For weekly-health, also installs a Fairwind pre-warm job 30 minutes before send so
the scheduled generate hits same-day disk caches instead of cold-polling.
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
_PREWARM_LEAD_MINUTES = 30
_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_CRON_DOW_LABELS = {
    "mon-fri": "Mon–Fri",
    "*": "every day",
    "mon": "Mondays",
    "tue": "Tuesdays",
    "wed": "Wednesdays",
    "thu": "Thursdays",
    "fri": "Fridays",
    "sat": "Saturdays",
    "sun": "Sundays",
}


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()
    _log_jobs("start_scheduler")


def cron_minus_minutes(cron: str, minutes: int = _PREWARM_LEAD_MINUTES) -> str | None:
    """Shift a simple 5-field cron earlier by `minutes`.

    Supports integer minute/hour and a single DOW token (name or 0-7). Returns
    None for ranges/steps (e.g. `*/5`, `1-5`) so callers can skip pre-warm.
    When the shift crosses midnight, DOW moves back one day.
    """
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute_s, hour_s, dom, month, dow = parts
    try:
        minute = int(minute_s)
        hour = int(hour_s)
    except ValueError:
        return None
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return None

    # Only single DOW tokens — ranges/lists/steps need calendar-aware shifting.
    if any(ch in dow for ch in ("-", ",", "/", "*")):
        return None

    total = hour * 60 + minute - minutes
    wrapped = False
    if total < 0:
        total += 24 * 60
        wrapped = True
    new_hour, new_minute = divmod(total, 60)

    new_dow = dow
    if wrapped:
        shifted = _shift_dow(dow, -1)
        if shifted is None:
            return None
        new_dow = shifted
    return f"{new_minute} {new_hour} {dom} {month} {new_dow}"


def _shift_dow(dow: str, days: int) -> str | None:
    token = (dow or "").strip().lower()
    if token in _DOW_NAMES:
        idx = _DOW_NAMES.index(token)
        return _DOW_NAMES[(idx + days) % 7]
    if token.isdigit():
        # Standard cron: 0 and 7 are Sunday.
        n = int(token)
        if n == 7:
            n = 0
        if not (0 <= n <= 6):
            return None
        return str((n + days) % 7)
    return None


def reschedule() -> None:
    """(Re)install cron jobs from every enabled pipeline row."""
    from designops.core.bootstrap import _normalize_weekday_cron
    from designops.core.db import session_scope
    from designops.core.models import Pipeline

    with session_scope() as s:
        rows = s.query(Pipeline).all()
        specs = []
        for p in rows:
            cron = _normalize_weekday_cron(p.schedule_cron) or p.schedule_cron
            if cron and cron != p.schedule_cron:
                p.schedule_cron = cron
                s.add(p)
            specs.append(
                {
                    "key": p.key,
                    "cron": cron,
                    "tz": p.timezone or "Europe/Riga",
                    "enabled": bool(p.enabled and cron),
                }
            )

    known_ids = {
        j.id
        for j in _scheduler.get_jobs()
        if not str(j.id).endswith(":once")  # keep one-shot test jobs
    }
    wanted: set[str] = set()
    for spec in specs:
        job_id = spec["key"]
        prewarm_id = f"{job_id}:prewarm"
        if not spec["enabled"]:
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            if _scheduler.get_job(prewarm_id):
                _scheduler.remove_job(prewarm_id)
            continue
        try:
            trigger = CronTrigger.from_crontab(
                spec["cron"], timezone=ZoneInfo(spec["tz"])
            )
        except (ValueError, KeyError) as e:
            print(f"  ⚠ cron invalid for {job_id}: {spec['cron']!r} ({e})", flush=True)
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            if _scheduler.get_job(prewarm_id):
                _scheduler.remove_job(prewarm_id)
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

        if job_id == "weekly-health":
            prewarm_cron = cron_minus_minutes(spec["cron"], _PREWARM_LEAD_MINUTES)
            if not prewarm_cron:
                print(
                    f"  ⚠ {prewarm_id} skipped — cannot derive pre-warm from "
                    f"cron={spec['cron']!r}",
                    flush=True,
                )
                if _scheduler.get_job(prewarm_id):
                    _scheduler.remove_job(prewarm_id)
            else:
                try:
                    prewarm_trigger = CronTrigger.from_crontab(
                        prewarm_cron, timezone=ZoneInfo(spec["tz"])
                    )
                except (ValueError, KeyError) as e:
                    print(
                        f"  ⚠ {prewarm_id} cron invalid: {prewarm_cron!r} ({e})",
                        flush=True,
                    )
                    if _scheduler.get_job(prewarm_id):
                        _scheduler.remove_job(prewarm_id)
                else:
                    wanted.add(prewarm_id)
                    _scheduler.add_job(
                        _run_prewarm,
                        prewarm_trigger,
                        id=prewarm_id,
                        replace_existing=True,
                        kwargs={"pipeline_key": job_id},
                        misfire_grace_time=3600,
                        coalesce=True,
                        max_instances=1,
                    )
                    pw = _scheduler.get_job(prewarm_id)
                    print(
                        f"  ⏰ scheduled {prewarm_id} cron={prewarm_cron!r} "
                        f"next={getattr(pw, 'next_run_time', None)} "
                        f"(Fairwind cache {_PREWARM_LEAD_MINUTES}m before send)",
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


def describe_schedule(cron: str | None, timezone: str = "Europe/Riga") -> str:
    """Human-readable cron, e.g. '12:00 Riga · Mon–Fri'."""
    raw = (cron or "").strip()
    if not raw:
        return "Not scheduled"
    parts = raw.split()
    if len(parts) < 5:
        return raw
    try:
        minute, hour = int(parts[0]), int(parts[1])
        time_s = f"{hour:02d}:{minute:02d}"
    except ValueError:
        return raw
    dow = parts[4].strip().lower()
    # APScheduler numeric DOW is Mon=0 — never label bare 1-5 as Mon–Fri.
    if dow == "1-5":
        days = "Tue–Sat (fix: use mon-fri)"
    else:
        days = _CRON_DOW_LABELS.get(dow, dow)
    tz_label = (timezone or "Europe/Riga").split("/")[-1].replace("_", " ")
    return f"{time_s} {tz_label} · {days}"


def format_countdown(
    next_run: datetime | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Short relative time until the next scheduled fire, e.g. 'in 2h 15m'."""
    if next_run is None:
        return None
    tz = next_run.tzinfo or _TZ
    now = now or datetime.now(tz)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=tz)
    delta = next_run - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "due now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return "in " + " ".join(parts)


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
    """Previous Mon–Fri day. Monday → Friday (weekend skipped)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


def scheduled_report_date(pipeline_key: str, fire_at: datetime | None) -> datetime.date | None:
    """Which report date a scheduled fire will generate.

    Daily digest always covers the previous working day — so Monday noon sends
    Friday's report; Tuesday sends Monday's; Friday sends Thursday's.
    """
    if fire_at is None:
        return None
    day = fire_at.astimezone(_TZ).date() if fire_at.tzinfo else fire_at.date()
    if pipeline_key == "daily-digest":
        return _prev_working_day(day)
    if pipeline_key == "weekly-backlog":
        from designops.pipelines.weekly_availability import resolve_week_monday

        return resolve_week_monday(day)
    if pipeline_key == "weekly-health":
        return day
    return None


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


def _run_prewarm(pipeline_key: str = "weekly-health") -> None:
    """Warm Fairwind disk caches only — no PipelineRun, no email."""
    if pipeline_key != "weekly-health":
        return
    from designops.core.db import session_scope
    from designops.core.models import Pipeline
    from designops.pipelines.weekly_health import prewarm_fairwind_caches

    with session_scope() as s:
        p = s.query(Pipeline).filter_by(key=pipeline_key).one_or_none()
        if not p or not p.enabled:
            print(f"  ⏭ {pipeline_key}:prewarm skipped — pipeline disabled", flush=True)
            return

    try:
        prewarm_fairwind_caches(reuse=True)
    except Exception as e:  # noqa: BLE001 — send job will cold-pull if needed
        _log.exception("%s pre-warm crashed", pipeline_key)
        print(f"  ⚠ {pipeline_key}:prewarm failed: {e}", flush=True)


def _attempt_scheduled(pipeline_key: str, attempt: int) -> tuple[bool, str | None, str]:
    """One generate(+deliver) attempt.

    Commits a ``running`` PipelineRun *before* heavy work so the Runs UI shows
    status immediately (same pattern as the manual Generate buttons).
    Returns (ok, run_id, detail).
    """
    from designops.core.db import session_scope
    from designops.core.enums import RunStatus
    from designops.core.models import Artifact, Pipeline, PipelineRun

    today = datetime.now(_TZ).date()
    run_id: str | None = None

    try:
        # --- Phase 1: create pending run and commit (visible in UI) ----------
        with session_scope() as s:
            p = s.query(Pipeline).filter_by(key=pipeline_key).one_or_none()
            if not p or not p.enabled:
                return True, None, "pipeline disabled — skip"
            send_mode = p.send_mode

            if pipeline_key == "daily-digest":
                from designops.pipelines.daily_digest import create_pending_run

                run = create_pending_run(s, _prev_working_day(today))
            elif pipeline_key == "weekly-backlog":
                from designops.pipelines.weekly_availability import resolve_week_monday
                from designops.pipelines.weekly_backlog import create_pending_run

                run = create_pending_run(s, resolve_week_monday(today))
            elif pipeline_key == "weekly-health":
                from designops.pipelines.weekly_health import create_pending_run

                run = create_pending_run(s)
            else:
                return True, None, f"unknown pipeline {pipeline_key}"

            run.note = f"scheduled attempt {attempt}"
            s.flush()
            run_id = str(run.id)

        print(
            f"  ▶ scheduled {pipeline_key} run={run_id} status=running",
            flush=True,
        )

        # --- Phase 2: generate + deliver in a fresh session -----------------
        with session_scope() as s:
            run = s.get(PipelineRun, run_id)
            if run is None:
                return False, run_id, "pending run missing after commit"
            p = s.get(Pipeline, run.pipeline_id)
            if p is None:
                return False, run_id, "pipeline missing"

            if pipeline_key == "daily-digest":
                from designops.pipelines.daily_digest import execute_run

                execute_run(s, run, reuse_ingest=True)
            elif pipeline_key == "weekly-backlog":
                from designops.pipelines.weekly_backlog import execute_run

                execute_run(s, run, reuse_ingest=True)
            elif pipeline_key == "weekly-health":
                from designops.pipelines.weekly_health import execute_run

                execute_run(s, run, reuse_ingest=True)

            s.flush()
            if run.status == RunStatus.FAILED:
                return False, run_id, run.error or "run status=failed"

            art = (
                s.query(Artifact)
                .filter_by(run_id=run.id, kind="html")
                .order_by(Artifact.id.desc())
                .first()
            )
            delivery = art.delivery_status if art else None
            if (send_mode or "").lower() == "send" and delivery == "blocked_go_live":
                return True, run_id, "blocked_go_live — not retrying"

            if _delivery_succeeded(send_mode, delivery):
                return True, run_id, f"ok delivery={delivery or 'n/a'}"

            return (
                False,
                run_id,
                f"delivery={delivery or 'missing'} (want send_mode={send_mode})",
            )
    except Exception as e:  # noqa: BLE001 — keep failed attempt visible, then retry
        _log.exception("%s scheduled attempt %s crashed", pipeline_key, attempt)
        if run_id:
            try:
                with session_scope() as s:
                    run = s.get(PipelineRun, run_id)
                    if run is not None and run.status == RunStatus.RUNNING:
                        run.status = RunStatus.FAILED
                        run.error = str(e)[:2000]
                        run.finished_at = datetime.now(_TZ)
                        run.note = f"scheduled attempt {attempt} crashed"
                        s.add(run)
            except Exception:  # noqa: BLE001
                _log.exception("could not mark run %s failed", run_id)
            return False, run_id, f"{type(e).__name__}: {e}"
        fail_id = _record_scheduler_failure(pipeline_key, attempt, e)
        return False, fail_id, f"{type(e).__name__}: {e}"


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
