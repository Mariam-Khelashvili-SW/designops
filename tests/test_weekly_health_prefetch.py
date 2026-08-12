"""Unit tests for weekly-health parallel prefetch helpers."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from designops.pipelines.weekly_health import (
    _prefetch_concurrency,
    _prefetch_one_project,
)


def test_prefetch_concurrency_caps_at_project_count():
    settings = SimpleNamespace(fw_export_concurrency=8)
    assert _prefetch_concurrency(4, settings) == 4
    assert _prefetch_concurrency(12, settings) == 8
    assert _prefetch_concurrency(0, settings) == 1


def test_prefetch_one_project_parallel_fanout(monkeypatch):
    calls: list[str] = []

    def mark(name: str):
        calls.append(name)
        if name == "fairwind":
            return (
                {"invoice_fields": {}, "live_meta": {}, "comms": "ok"},
                [],
            )
        if name == "jira":
            return [], [], {}
        if name == "calls":
            return {"last_call_display": "n/a"}, {}
        if name == "figma":
            return {"excerpt": "(none)", "threads": [], "files": 0, "errors": []}
        raise AssertionError(name)

    monkeypatch.setattr(
        "designops.pipelines.weekly_health._fetch_fairwind_pair",
        lambda *a, **k: mark("fairwind"),
    )
    monkeypatch.setattr(
        "designops.pipelines.weekly_health._fetch_jira_bundle",
        lambda *a, **k: mark("jira"),
    )
    monkeypatch.setattr(
        "designops.pipelines.weekly_health._fetch_call_dates_bundle",
        lambda *a, **k: mark("calls"),
    )
    monkeypatch.setattr(
        "designops.pipelines.weekly_health._fetch_figma_bundle_for_project",
        lambda *a, **k: mark("figma"),
    )
    monkeypatch.setattr(
        "designops.pipelines.weekly_health.get_settings",
        lambda: SimpleNamespace(
            fairwind_configured=False,
            jira_configured=False,
        ),
    )

    proj = SimpleNamespace(
        canonical_name="Acer",
        fairwind_account_id="acct-1",
        jira_project_key="ACERP1",
        jira_scope=None,
        figma_urls=[],
    )
    name, payload, snippets = _prefetch_one_project(
        proj,  # type: ignore[arg-type]
        account_domains={"acct-1": ["acer.com"]},
        as_of=date(2026, 8, 11),
        comms_from=date(2026, 8, 4),
        roster_emails=set(),
        participant_emails=["olga@scandiweb.com"],
        reuse=True,
    )
    assert name == "Acer"
    assert payload["fairwind"]["comms"] == "ok"
    assert set(calls) == {"fairwind", "jira", "calls", "figma"}
