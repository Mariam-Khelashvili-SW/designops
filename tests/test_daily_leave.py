"""Daily digest leave: day-level effective_status + VACSICK sync before coverage."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from designops.core.enums import PersonStatus
from designops.core.identity import RosterIndex, effective_status
from designops.pipelines.daily_digest import _leave_context, _reconcile_availability


def test_effective_status_respects_leave_from_and_until():
    leave_from = date(2026, 8, 5)
    leave_until = date(2026, 8, 7)
    assert (
        effective_status(
            PersonStatus.ON_LEAVE, leave_until, date(2026, 8, 4), leave_from=leave_from
        )
        == PersonStatus.ACTIVE
    )
    assert (
        effective_status(
            PersonStatus.ON_LEAVE, leave_until, date(2026, 8, 5), leave_from=leave_from
        )
        == PersonStatus.ON_LEAVE
    )
    assert (
        effective_status(
            PersonStatus.ON_LEAVE, leave_until, date(2026, 8, 8), leave_from=leave_from
        )
        == PersonStatus.ACTIVE
    )
    # Weekly-style call (no leave_from): still on_leave on Mon before mid-week start
    assert (
        effective_status(PersonStatus.ON_LEAVE, leave_until, date(2026, 8, 3))
        == PersonStatus.ON_LEAVE
    )


def test_roster_index_day_level_leave():
    pid = uuid4()
    row = SimpleNamespace(
        id=pid,
        full_name="Kirill Rogovets",
        emails=["kirill@scandiweb.com"],
        jira_account_id="acct-k",
        status=PersonStatus.ON_LEAVE,
        leave_from=date(2026, 8, 5),
        leave_until=date(2026, 8, 7),
    )
    before = RosterIndex.from_rows([row], date(2026, 8, 4))
    during = RosterIndex.from_rows([row], date(2026, 8, 5))
    assert before.active_count == 1
    assert during.active_count == 0
    assert during._members[0].status == PersonStatus.ON_LEAVE


def test_reconcile_marks_on_leave_not_no_report():
    leave_person = SimpleNamespace(
        id=uuid4(),
        full_name="Kirill Rogovets",
        status=PersonStatus.ON_LEAVE,
        leave_from=date(2026, 8, 4),
        leave_until=date(2026, 8, 4),
    )
    silent = SimpleNamespace(
        id=uuid4(),
        full_name="Arturs Boroviks",
        status=PersonStatus.ACTIVE,
        leave_from=None,
        leave_until=None,
    )
    filtered = SimpleNamespace(
        silent_person_ids={silent.id},
        reported_person_ids=set(),
    )
    digest = {
        "no_report": [],
        "at_a_glance": {},
        "needs_review": [],
        "status": [],
        "open_questions": [],
        "todays_plans": [],
    }
    _reconcile_availability(
        digest, filtered, [leave_person, silent], date(2026, 8, 4)
    )
    by_name = {r["name"]: r for r in digest["no_report"]}
    assert by_name["Kirill Rogovets"]["status"] == "on_leave"
    assert by_name["Kirill Rogovets"]["context"] == "On leave Tue 4 Aug."
    assert by_name["Arturs Boroviks"]["status"] == "no_report"
    assert by_name["Arturs Boroviks"]["context"] in (None, "")
    assert digest["at_a_glance"]["no_report"] == 1
    assert digest["at_a_glance"]["active"] == 0
    assert digest["at_a_glance"]["reported"] == 0


def test_leave_context_range():
    p = SimpleNamespace(
        leave_from=date(2026, 8, 3),
        leave_until=date(2026, 8, 5),
    )
    assert _leave_context(p) == "On leave from Mon 3 Aug (through 5 Aug)."


def test_daily_execute_syncs_vacsick(monkeypatch):
    """execute_run calls sync_leave_from_vacsick for the report week before filtering."""
    from designops.core.enums import RunStatus
    from designops.pipelines import daily_digest as dd

    report_date = date(2026, 8, 4)  # Tuesday
    seen: dict = {}

    def fake_sync(people, *, week_monday, week_friday=None, reference_date=None, settings=None, client=None):
        seen["week_monday"] = week_monday
        seen["week_friday"] = week_friday
        seen["reference_date"] = reference_date
        seen["people"] = [p.full_name for p in people]
        return {
            "configured": True,
            "fetched": 1,
            "detections": [],
            "updated_names": ["Kirill Rogovets"],
            "note": None,
        }

    monkeypatch.setattr(dd, "sync_leave_from_vacsick", fake_sync)
    monkeypatch.setattr(dd, "_ingest", lambda *a, **k: ([], {"exports_failed": 0}))
    monkeypatch.setattr(
        dd,
        "filter_corpus",
        lambda *a, **k: SimpleNamespace(
            silent_person_ids=set(),
            reported_person_ids=set(),
            unmatched_projects={},
            coverage_ratio=1.0,
            counts=lambda: {},
            audit=[],
            included=[],
            beyond_daily=[],
        ),
    )
    monkeypatch.setattr(
        dd,
        "_synthesize",
        lambda *a, **k: (
            {
                "at_a_glance": {
                    "active": 0,
                    "need_review": 0,
                    "blocked": 0,
                    "no_report": 0,
                },
                "status": [],
                "needs_review": [],
                "open_questions": [],
                "todays_plans": [],
                "no_report": [],
            },
            {
                "mode": "recorded",
                "model": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            },
        ),
    )
    monkeypatch.setattr(dd, "render_digest", lambda *a, **k: "<html/>")
    monkeypatch.setattr(
        dd,
        "deliver",
        lambda **k: SimpleNamespace(status="none", message_id=None),
    )
    monkeypatch.setattr(dd, "_persist_documents", lambda *a, **k: None)
    monkeypatch.setattr(dd, "_persist_flags", lambda *a, **k: None)
    monkeypatch.setattr(
        dd,
        "get_settings",
        lambda: SimpleNamespace(
            min_coverage=0.5,
            setup_owner_email="ops@example.com",
            jira_configured=False,
            fairwind_configured=False,
        ),
    )

    person = SimpleNamespace(
        id=uuid4(),
        full_name="Kirill Rogovets",
        emails=["k@scandiweb.com"],
        jira_account_id="acct-k",
        status=PersonStatus.ACTIVE,
        leave_from=None,
        leave_until=None,
    )
    pipeline = SimpleNamespace(
        id=uuid4(),
        go_live=False,
        send_mode="none",
        recipients=[],
    )
    run = SimpleNamespace(
        id=uuid4(),
        pipeline_id=pipeline.id,
        report_date=report_date,
        ingest_batch_id=None,
        status=RunStatus.RUNNING,
        error=None,
        finished_at=None,
        counts=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        skill_version=None,
        note=None,
    )

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter_by(self, **kw):
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return None

        def all(self):
            return list(self._rows)

        def one(self):
            return self._rows[0]

    class FakeSession:
        def get(self, model, _id):
            if model.__name__ == "Pipeline":
                return pipeline
            return None

        def query(self, model):
            name = getattr(model, "__name__", str(model))
            if name == "Person":
                return FakeQuery([person])
            if name == "Project":
                return FakeQuery([])
            if name == "Account":
                return FakeQuery([])
            if name == "IngestBatch":
                return FakeQuery([])
            return FakeQuery([])

        def add(self, _obj):
            return None

        def flush(self):
            seen["flushed"] = True

    dd.execute_run(FakeSession(), run, reuse_ingest=False)
    assert seen["week_monday"] == date(2026, 8, 3)
    assert seen["week_friday"] == date(2026, 8, 7)
    assert seen["reference_date"] == report_date
    assert "Kirill Rogovets" in seen["people"]
    assert seen.get("flushed") is True
    assert run.status in (RunStatus.OK, RunStatus.FLAGGED)
