from types import SimpleNamespace

from designops.api.run_status import explain_run_status, status_why


def test_ok_run_has_no_why():
    run = SimpleNamespace(status="ok", error=None, counts={})
    assert explain_run_status(run, min_coverage=0.6) == []
    assert status_why(run, min_coverage=0.6) == ""


def test_flagged_explains_coverage_floor():
    run = SimpleNamespace(
        status="flagged",
        error=None,
        counts={
            "coverage_ratio": 0.57,
            "reported": 4,
            "silent": 3,
            "unmatched_projects": 0,
            "coverage": {},
        },
    )
    why = explain_run_status(run, min_coverage=0.6)
    assert len(why) == 1
    assert "57%" in why[0]
    assert "60%" in why[0]
    assert "4 reported" in why[0]
    assert "3 silent" in why[0]


def test_flagged_explains_export_failures():
    run = SimpleNamespace(
        status="flagged",
        error=None,
        counts={"coverage_ratio": 0.9, "coverage": {"exports_failed": 2}},
    )
    why = explain_run_status(run, min_coverage=0.6)
    assert why == ["2 Fairwind exports failed"]


def test_failed_uses_error():
    run = SimpleNamespace(status="failed", error="ANTHROPIC_API_KEY missing", counts={})
    assert explain_run_status(run, min_coverage=0.6) == ["ANTHROPIC_API_KEY missing"]
