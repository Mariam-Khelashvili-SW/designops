"""Unit tests for A2 Weekly Project Health & Budget math + render."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from designops.pipelines.render import render_weekly_health
from designops.pipelines.weekly_health_math import (
    apply_jira_scope,
    build_project_burn,
    burn_pct,
    client_action_ageing,
    design_roster_emails,
    enrich_ticket,
    glance_kpis,
    in_design_scope,
    is_epic,
    is_timelog_bucket,
    pct_class,
    project_jira_links,
    working_days_between,
)


def test_pct_class_bands():
    assert pct_class(0, 10, is_done=False) == "mut"
    assert pct_class(5, 10, is_done=True) == "done"
    assert pct_class(10, 10, is_done=False) == "bad"
    assert pct_class(9, 10, is_done=False) == "warn"
    assert pct_class(7, 10, is_done=False) == "watch"
    assert pct_class(5, 10, is_done=False) == "ok"
    assert burn_pct(5, 10) == 50
    assert burn_pct(0, 10) is None


def test_design_scope_component_or_roster():
    roster = {"arturs.boroviks@scandiweb.com", "dorota@scandiweb.com"}
    # Component match, even if assignee not on roster
    assert in_design_scope(
        {
            "issue_type": "Task",
            "components": ["UX/UI"],
            "assignee_email": "dev.person@scandiweb.com",
            "original_hours": 8,
            "spent_hours": 2,
        },
        roster,
    )
    # Roster match without component
    assert in_design_scope(
        {
            "issue_type": "Task",
            "components": [],
            "assignee_email": "arturs.boroviks@scandiweb.com",
            "original_hours": 8,
            "spent_hours": 2,
        },
        roster,
    )
    # Dev assignee, no design component
    assert not in_design_scope(
        {
            "issue_type": "Task",
            "components": ["Backend"],
            "assignee_email": "vladislavs.kapustjonoks@scandiweb.com",
            "original_hours": 8,
            "spent_hours": 2,
        },
        roster,
    )
    # Elene Minashvili (PM) must not match via substring if not on roster
    assert not in_design_scope(
        {
            "issue_type": "Task",
            "components": [],
            "assignee_email": "elene.minashvili@scandiweb.com",
            "original_hours": 4,
            "spent_hours": 1,
        },
        {"elene.chekurishvili@scandiweb.com"},
    )


def test_exclude_epic_and_timelog():
    assert is_epic({"issue_type": "Epic"})
    assert is_timelog_bucket({"issue_type": "Time Logs", "original_hours": 40, "spent_hours": 10})
    assert is_timelog_bucket({"issue_type": "Task", "original_hours": 1, "spent_hours": 5})
    assert not in_design_scope(
        {
            "issue_type": "Epic",
            "components": ["DESIGN"],
            "assignee_email": "arturs@scandiweb.com",
            "original_hours": 100,
            "spent_hours": 0,
        },
        {"arturs@scandiweb.com"},
    )


def test_working_days_and_ageing():
    # Wed 15 Jul → Fri 17 Jul = 3 working days inclusive
    assert working_days_between(date(2026, 7, 15), date(2026, 7, 17)) == 3
    # Mon 13 → Fri 17 = 5
    assert working_days_between(date(2026, 7, 13), date(2026, 7, 17)) == 5

    tickets = [
        {
            "key": "ACERP1-40",
            "status": "Client Action",
            "client_action_since": date(2026, 7, 15),
            "original_hours": 16,
            "spent_hours": 9,
        },
        {
            "key": "ACERP1-45",
            "status": "Client Action",
            "client_action_since": date(2026, 7, 17),
            "original_hours": 24,
            "spent_hours": 7,
        },
    ]
    aged = client_action_ageing(tickets, as_of=date(2026, 7, 17), threshold_working_days=3)
    # 15→17 is 3 days inclusive; threshold is > 3, so neither ages yet on Fri 17
    assert aged == []
    aged2 = client_action_ageing(tickets, as_of=date(2026, 7, 20), threshold_working_days=3)
    # Mon 20: Wed 15 → Mon 20 = Wed,Thu,Fri,Mon = 4 working days > 3
    assert any(t["key"] == "ACERP1-40" for t in aged2)


def test_jira_scope_key_window():
    tickets = [
        {"key": "UOM-275", "summary": "old"},
        {"key": "UOM-482", "summary": "base"},
        {"key": "UOM-490", "summary": "edge"},
        {"key": "UOM-491", "summary": "after"},
    ]
    scoped = apply_jira_scope(tickets, {"key_min": 481, "key_max": 490})
    assert [t["key"] for t in scoped] == ["UOM-482", "UOM-490"]


def test_jira_scope_epic_descendants():
    tickets = [
        {"key": "UOM-275", "parent_key": None, "summary": "old engagement"},
        {"key": "UOM-481", "parent_key": None, "summary": "epic", "issue_type": "Epic"},
        {"key": "UOM-482", "parent_key": "UOM-481", "summary": "child"},
        {"key": "UOM-500", "parent_key": "UOM-482", "summary": "grandchild subtask"},
        {"key": "UOM-600", "parent_key": "UOM-275", "summary": "other epic child"},
    ]
    scoped = apply_jira_scope(tickets, {"epic_key": "UOM-481"})
    assert [t["key"] for t in scoped] == ["UOM-481", "UOM-482", "UOM-500"]


def test_build_burn_and_glance():
    tickets = [
        {
            "key": "ACERP1-49",
            "summary": "Global elements",
            "status": "In Progress",
            "status_category": "In Progress",
            "original_hours": 6,
            "spent_hours": 6,
            "assignee_display": "Arturs Boroviks",
            "assignee_email": "arturs@scandiweb.com",
            "components": ["DESIGN"],
            "issue_type": "Task",
            "status_entries": [],
        },
        {
            "key": "ACERP1-58",
            "summary": "PLP designs",
            "status": "In Progress",
            "original_hours": 20,
            "spent_hours": 0,
            "assignee_display": "Arturs Boroviks",
            "assignee_email": "arturs@scandiweb.com",
            "components": ["UX"],
            "issue_type": "Task",
            "status_entries": [],
        },
    ]
    card = build_project_burn(
        display_name="Acer",
        subtitle="Hyvä Redesign · Phase 1",
        signed_estimate_h=904,
        agreement={
            "invoiced_to_date_label": "n/a",
            "invoiced_to_date_sub": "no invoices",
            "signed_estimate_sub": "setup baseline",
        },
        tickets=tickets,
        as_of=date(2026, 7, 17),
    )
    assert card["logged_h"] == 6.0
    assert card["over_est_count"] == 1  # ACERP1-49 at 100% still in progress
    assert card["signed_estimate_display"] == "904h"
    assert card["done_count"] == 0
    assert card["done_pct"] == 0
    assert card["done_est_h"] == 0
    assert card["done_est_pct"] == 0  # 0h done of 26h estimated
    assert any(g["status"] == "In Progress" for g in card["groups"])

    g = glance_kpis([card, {"pending": True, "display_name": "SGD"}])
    assert g["reported"] == 1
    assert g["total"] == 2
    assert g["over_est"] == 1


def test_design_roster_emails_exact():
    people = [
        SimpleNamespace(emails=["Arturs.Boroviks@scandiweb.com", "a@x.com"]),
        SimpleNamespace(emails=[]),
    ]
    emails = design_roster_emails(people)
    assert "arturs.boroviks@scandiweb.com" in emails
    assert "a@x.com" in emails


def test_render_weekly_health_html():
    card = build_project_burn(
        display_name="Tobis UAB",
        subtitle="B2B Magento · Phase 1 discovery",
        signed_estimate_h=187,
        agreement={
            "contract_type": "fixed_fee",
            "phase_fee_eur": 12000,
        },
        tickets=[
            {
                "key": "TOB-7",
                "summary": "UX & IA — Homepage",
                "status": "New",
                "original_hours": 20,
                "spent_hours": 6,
                "assignee_display": "Tamari Giunashvili",
                "assignee_email": "tamari@scandiweb.com",
                "components": ["UX"],
                "issue_type": "Task",
                "status_entries": [],
                "url": "https://scandiflow.atlassian.net/browse/TOB-7",
            }
        ],
        as_of=date(2026, 7, 17),
    )
    # Live Fairwind unpaid UX/UI overlay (orchestrator normally sets this)
    card["invoiced_label"] = "Not invoiced yet"
    card["invoiced_muted"] = True
    card["invoiced_sub"] = "no UX/UI invoices in Fairwind"
    card["verdict"] = "Healthy and early."
    card["highlights"] = []
    digest = {
        "at_a_glance": glance_kpis([card]),
        "projects": [card],
        "actions": [
            {
                "text": "Get Tobis ticket status corrected. Ask Tamari to move tickets.",
                "project": "Tobis",
                "evidence": 'email "Wireframe feedback", 16 Jul',
            }
        ],
    }
    html = render_weekly_health(digest, date(2026, 7, 17), sample=True, coverage={})
    assert "Weekly Project Health" in html
    assert "projects reported" not in html
    assert "in client action" in html
    assert "Invoiced UX/UI" in html
    assert "Not invoiced yet" in html
    assert "Done tickets" in html
    assert "0 / 1" in html
    assert "0h of 20h est" in html
    assert "table.t td.p.ok" in html or "class=\"p " in html
    assert "Where your action is needed" in html
    assert "Wireframe feedback" in html
    assert "TOB-7" in html
    assert "SAMPLE / DRY-RUN" in html
    assert "Billing note" not in html
    assert "kick-off milestone" not in html


def test_render_weekly_health_figma_panel():
    card = build_project_burn(
        display_name="Acer",
        subtitle="Wireframes",
        signed_estimate_h=100,
        agreement={},
        tickets=[],
        as_of=date(2026, 8, 11),
    )
    card["figma"] = {
        "has_comments": True,
        "counts": {
            "new_comments": 17,
            "new_from_client": 6,
            "resolved_this_week": 19,
            "still_open": 2,
            "overdue_items": 2,
        },
        "overdue": [
            {
                "who": "Artur B",
                "to": "Svitlana Madei",
                "date_label": "27 Jul",
                "age_working_days": 11,
                "quote": "Can you check comment, is it possible to set custom color…",
                "quotes": ["Can you check comment, is it possible to set custom color…"],
                "link": "https://www.figma.com/design/abc123/Acer?node-id=1-2",
                "kind": "UNANSWERED",
            }
        ],
        "this_week": [],
    }
    html = render_weekly_health(
        {
            "at_a_glance": glance_kpis([card], figma_overdue=2),
            "projects": [card],
            "actions": [],
        },
        date(2026, 8, 11),
        sample=True,
        coverage={},
    )
    assert "Figma comments" in html
    assert "open pin" in html
    assert "Open more than a week" in html
    assert "Figma items open" in html
    assert "2" in html
    assert "Figma feedback" not in html
    assert "Waiting on our reply" not in html


def test_is_plain_figma_risk_filters_count_jargon():
    from designops.pipelines.weekly_health_figma import is_plain_figma_risk

    assert is_plain_figma_risk(
        "Homepage header still broken — fix claimed but not confirmed"
    )
    assert not is_plain_figma_risk(
        "12 client comments unacked >24h; oldest open since 27 Jul"
    )


def test_total_invoiced_ux_summary():
    from designops.pipelines.weekly_health_invoices import summarize_ux_invoiced

    invoices = [
        {
            "invoice_number": "SCAE-CPY-05",
            "name": "Discovery June",
            "payment_status": "Not Paid",
            "outstanding_balance": "7310.0",
            "currency_iso_code": "EUR",
            "line_items": [
                {
                    "name": "SEO",
                    "line_service_supplied": "SEO, Discovery June 2026",
                    "line_total_price": "437.5",
                },
                {
                    "name": "UX/UI",
                    "line_service_supplied": "UX/UI, Discovery June 2026",
                    "line_quantity": "55.58",
                    "line_total_price": "3890.6",
                },
            ],
        },
        {
            "invoice_number": "SCAE-CPY-03",
            "name": "Discovery May",
            "payment_status": "Paid",
            "outstanding_balance": "0.0",
            "currency_iso_code": "EUR",
            "line_items": [
                {
                    "name": "UX/UI",
                    "line_service_supplied": "UX/UI, Discovery May 2026",
                    "line_quantity": "52.33",
                    "line_total_price": "3663.1",
                },
            ],
        },
    ]
    # Total invoiced UX/UI across all invoices, paid or not (SEO line excluded).
    summary = summarize_ux_invoiced(invoices)
    assert summary["invoiced_label"] == "€7,553.70"
    assert summary["invoiced_muted"] is False
    assert summary["invoiced_sub"].startswith("107.91h · 2 invoices")
    assert "SCAE-CPY-05" in summary["invoiced_sub"]
    assert len(summary["ux_invoice_lines"]) == 2
    assert summary["ux_invoiced_total"] == 7553.7
    assert summary["ux_invoiced_hours"] == 107.91
    assert "unpaid" not in summary["invoiced_sub"].lower()

    # Single invoice → hours + invoice number + line detail
    single = summarize_ux_invoiced([invoices[1]])
    assert single["invoiced_label"] == "€3,663.10"
    assert single["invoiced_sub"] == "52.33h · SCAE-CPY-03 · UX/UI, Discovery May 2026"

    # No invoices at all → honest "not invoiced yet"
    empty = summarize_ux_invoiced([])
    assert empty["invoiced_label"] == "Not invoiced yet"
    assert empty["invoiced_sub"] == "no UX/UI invoices in Fairwind"


def test_build_project_burn_does_not_use_seed_invoices():
    card = build_project_burn(
        display_name="Acer",
        subtitle="",
        signed_estimate_h=904,
        agreement={
            "invoiced_to_date_label": "€999,999",
            "invoiced_to_date_sub": "FAKE",
            "invoice_notes": "should not appear",
            "signed_estimate_sub": "fake sub",
        },
        tickets=[],
        as_of=date(2026, 7, 17),
    )
    assert card["invoiced_label"] == "n/a"
    assert card["invoice_notes"] is None
    assert card["signed_estimate_sub"] == "signed design hours"
    assert "FAKE" not in (card["invoiced_sub"] or "")


def test_live_project_meta_from_fairwind_shapes():
    from designops.pipelines.weekly_health_commercial import (
        short_agreement_title,
        summarize_live_project_meta,
    )

    assert (
        short_agreement_title("2026_SCAE_CPYou (Acer)_Phase 1 SOW") == "Phase 1 SOW"
    )
    assert (
        short_agreement_title("2026_SCAE_TOBIS_Phase 1 SOW [No. 1]")
        == "Phase 1 SOW [No. 1]"
    )

    meta = summarize_live_project_meta(
        [
            {
                "name": "2026_SCAE_CPYou (Acer)_NDA",
                "document_type": "NDA (Non-Disclosure Agreement)",
                "document_date": "2026-01-08 00:00:00",
            },
            {
                "name": "2026_SCAE_CPYou (Acer)_Phase 1 SOW",
                "document_type": "Discovery Agreement",
                "document_date": "2026-04-03 00:00:00",
            },
        ],
        [
            {
                "name": "CPYOU B.V. - (ACERP1) Discovery in June 2026",
                "stage_name": "Closed Won",
                "cooperation": "Discovery (Billable)",
                "jira_key": "ACERP1",
                "amount": 7310.0,
                "hours_sum_resources": 0.0,
            },
            {
                "name": "Acer - Support - Fixed - Hyva implementation and UX Redesign",
                "stage_name": "Intro Meeting",
                "amount": 200000.0,
                "hours_sum_resources": 0.0,
            },
        ],
        jira_key="ACERP1",
    )
    assert meta["subtitle"] == "Phase 1 SOW"
    assert meta["agreement"]["sow_name"] == "Phase 1 SOW"
    assert meta["agreement"]["cooperation"] == "Discovery (Billable)"
    assert meta["agreement"]["amount_eur"] == 7310.0
    # Fairwind has no structured signed hours → honest n/a upstream
    assert meta["signed_design_estimate_h"] is None


def test_signed_hours_only_from_fairwind_resource_total():
    from designops.pipelines.weekly_health_commercial import summarize_live_project_meta

    meta = summarize_live_project_meta(
        [],
        [
            {
                "name": "Example Phase 1",
                "stage_name": "Won",
                "hours_sum_resources": 120.0,
                "amount": 10000,
            }
        ],
    )
    assert meta["signed_design_estimate_h"] == 120.0
    assert meta["subtitle"]  # from opportunity fallback


def test_monthly_billing_opps_never_become_signed_estimate():
    from designops.pipelines.weekly_health_commercial import summarize_live_project_meta

    meta = summarize_live_project_meta(
        [],
        [
            {
                # Whole-team monthly resourcing — not a signed design estimate
                "name": "THE REGENTS OF THE UNIVERSITY OF MICHIGAN - (UOM) Dedicated in March 2025",
                "stage_name": "Closed Won",
                "hours_sum_resources": 231.58333333333334,
            },
            {
                "name": "INV OPP - THE REGENTS OF THE UNIVERSITY OF MICHIGAN - Jun 2026 - UOM",
                "stage_name": "Closed Won",
                "hours_sum_resources": 99.0,
            },
        ],
    )
    assert meta["signed_design_estimate_h"] is None


def test_fmt_hours_rounds_to_two_decimals():
    from designops.pipelines.weekly_health_math import fmt_hours

    assert fmt_hours(None) == "n/a"
    assert fmt_hours(904) == "904h"
    assert fmt_hours(904.0) == "904h"
    assert fmt_hours(231.58333333333334) == "231.58h"
    assert fmt_hours(126.5) == "126.5h"


def test_commercial_cache_roundtrip(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from designops.pipelines import weekly_health_commercial as whc

    settings = SimpleNamespace(corpus_store_dir=str(tmp_path))
    aid = "acct-1"
    as_of = date(2026, 7, 17)
    payload = {
        "invoices": [{"invoice_number": "SCAE-1"}],
        "agreements": [{"name": "Phase 1 SOW"}],
        "opportunities": [],
    }
    whc.save_commercial_cache(settings, aid, as_of, payload)
    loaded = whc.load_commercial_cache(settings, aid, as_of)
    assert loaded == payload

    # reuse path should not call Fairwind
    class Boom:
        s = settings

        def create_export_range(self, *a, **k):
            raise AssertionError("should not hit Fairwind when cache exists")

    out, source = whc.fetch_salesforce_commercial(
        Boom(), aid, date_to=as_of, reuse=True, settings=settings
    )
    assert source == "fairwind-cached"
    assert out["invoices"][0]["invoice_number"] == "SCAE-1"


def test_project_jira_links_prefer_epic():
    links = project_jira_links(
        jira_project_key="UOM",
        jira_scope={"epic_key": "UOM-481"},
        base="https://scandiflow.atlassian.net",
    )
    assert links["epic_key"] == "UOM-481"
    assert links["epic_url"] == "https://scandiflow.atlassian.net/browse/UOM-481"
    assert links["jira_project_url"] == "https://scandiflow.atlassian.net/browse/UOM"
    assert links["jira_url"] == links["epic_url"]

    proj_only = project_jira_links(
        jira_project_key="ACERP1",
        jira_scope=None,
        base="https://scandiflow.atlassian.net",
    )
    assert proj_only["jira_url"] == "https://scandiflow.atlassian.net/browse/ACERP1"
    assert proj_only["epic_url"] is None


def test_enrich_ticket_status_since_and_stale():
    t = enrich_ticket(
        {
            "key": "UOM-482",
            "summary": "Base Template",
            "status": "In Progress",
            "original_hours": 10,
            "spent_hours": 2,
            "assignee_display": "Kirill Rogovets",
            "status_entries": [
                {"from": "New", "to": "To Do", "at": "2026-07-01T10:00:00.000+0300"},
                {
                    "from": "To Do",
                    "to": "In Progress",
                    "at": "2026-07-10T12:00:00.000+0300",
                },
            ],
        },
        as_of=date(2026, 7, 24),
    )
    assert t["status_since"] == date(2026, 7, 10)
    assert t["days_in_status"] == 14
    assert t["status_stale"] == "red"

    mid = enrich_ticket(
        {
            "key": "X-1",
            "status": "Client Action",
            "original_hours": 5,
            "spent_hours": 1,
            "status_entries": [
                {"from": "In Progress", "to": "Client Action", "at": "2026-07-16"}
            ],
        },
        as_of=date(2026, 7, 24),
    )
    assert mid["days_in_status"] == 8
    assert mid["status_stale"] == "amber"
    assert mid["client_action_since"] == date(2026, 7, 16)

    # No status changelog → age from created (not updated).
    never = enrich_ticket(
        {
            "key": "Y-1",
            "status": "To Do",
            "original_hours": 3,
            "spent_hours": 0,
            "status_entries": [],
            "created": "2026-07-01T09:00:00.000+0300",
            "updated": "2026-07-20T18:00:00.000+0300",
        },
        as_of=date(2026, 7, 24),
    )
    assert never["status_since"] == date(2026, 7, 1)
    assert never["days_in_status"] == 23
    assert never["status_stale"] == "red"

    # Client Action with no transition history → created date.
    ca_born = enrich_ticket(
        {
            "key": "Z-1",
            "status": "Client Action",
            "original_hours": 2,
            "spent_hours": 0,
            "status_entries": [],
            "created": "2026-07-18T10:00:00.000+0300",
        },
        as_of=date(2026, 7, 24),
    )
    assert ca_born["client_action_since"] == date(2026, 7, 18)
    assert ca_born["days_in_status"] == 6


def test_derive_call_dates_from_calendar_meetings():
    from designops.pipelines.weekly_health_meetings import (
        derive_call_dates_from_meetings,
        design_participant_emails,
        normalize_email_domains,
    )

    assert normalize_email_domains(["Acer.com", "acer.com", "", "store.acer.com"]) == [
        "acer.com",
        "store.acer.com",
    ]
    participants = design_participant_emails(
        ["arturs.boroviks@scandiweb.com", "dorota.umiastowska@scandiweb.com"],
        settings=SimpleNamespace(olga_email="olga@scandiweb.com"),
    )
    assert "olga@scandiweb.com" in participants
    assert "arturs.boroviks@scandiweb.com" in participants

    as_of = date(2026, 7, 24)
    items = [
        {
            "summary": "Acer UX review",
            "status": "confirmed",
            "startTime": "2026-05-22T08:00:00.000Z",
            "isUpcoming": False,
        },
        {
            "summary": "Cancelled sync",
            "status": "cancelled",
            "startTime": "2026-07-20T10:00:00.000Z",
            "isUpcoming": False,
        },
        {
            "summary": "Next Acer workshop",
            "status": "confirmed",
            "startTime": "2026-08-03T09:00:00.000Z",
            "isUpcoming": True,
        },
    ]
    fields = derive_call_dates_from_meetings(items, as_of=as_of)
    assert fields["last_call_date"] == "2026-05-22"
    assert "22 May" in fields["last_call_display"]
    assert fields["next_call_date"] == "2026-08-03"
    assert fields["calls_muted"] is False


def test_call_dates_for_domains_uses_transcript_api(monkeypatch):
    from designops.pipelines import weekly_health_meetings as whm

    class FakeSettings:
        transcript_api_base_url = "http://localhost:3001"
        transcript_api_token = "test-token"

        @property
        def transcript_api_configured(self):
            return True

    def fake_fetch(**kwargs):
        assert kwargs["email_domains"] == ["acer.com"]
        assert "olga@scandiweb.com" in kwargs["participant_emails"]
        return {
            "total": 1,
            "truncated": False,
            "items": [
                {
                    "summary": "Acer call",
                    "status": "confirmed",
                    "startTime": "2026-07-10T10:00:00.000Z",
                    "isUpcoming": False,
                }
            ],
        }

    monkeypatch.setattr(whm, "fetch_calendar_meetings", fake_fetch)
    fields, meta = whm.call_dates_for_domains(
        ["acer.com"],
        ["olga@scandiweb.com", "arturs.boroviks@scandiweb.com"],
        as_of=date(2026, 7, 24),
        settings=FakeSettings(),
    )
    assert fields["last_call_date"] == "2026-07-10"
    assert meta["source"] == "transcript-calendar"
    assert meta["domains"] == ["acer.com"]


def test_render_includes_jira_link_days_and_calls():
    card = build_project_burn(
        display_name="University of Michigan",
        subtitle="CRO UX/UI",
        signed_estimate_h=135,
        agreement={},
        tickets=[
            {
                "key": "UOM-482",
                "summary": "Base Template",
                "status": "In Progress",
                "original_hours": 10,
                "spent_hours": 2,
                "assignee_display": "Kirill Rogovets",
                "components": ["UX"],
                "issue_type": "Task",
                "status_entries": [
                    {"from": "To Do", "to": "In Progress", "at": "2026-07-10"}
                ],
                "url": "https://scandiflow.atlassian.net/browse/UOM-482",
            }
        ],
        as_of=date(2026, 7, 24),
    )
    card.update(
        project_jira_links(
            jira_project_key="UOM",
            jira_scope={"epic_key": "UOM-481"},
            base="https://scandiflow.atlassian.net",
        )
    )
    card.update(
        {
            "last_call_display": "Fri 18 Jul",
            "next_call_display": "Mon 3 Aug",
            "calls_muted": False,
            "last_call_title": "UMich weekly sync",
            "verdict": "On track.",
        }
    )
    html = render_weekly_health(
        {"at_a_glance": glance_kpis([card]), "projects": [card], "actions": []},
        date(2026, 7, 24),
        sample=True,
        coverage={},
    )
    assert 'href="https://scandiflow.atlassian.net/browse/UOM-481"' in html
    assert "135h" in html
    assert ">Days<" in html
    assert "14d" in html
    assert "Last call" in html
    assert "Fri 18 Jul" in html
    assert "Next" in html
    assert "Mon 3 Aug" in html

