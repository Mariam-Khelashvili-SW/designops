"""Unit tests for design intake pipeline (no Anthropic / no Notion / no DB)."""

from __future__ import annotations

import io

import pytest

from designops.adapters.notion import (
    DESIGN_PROJECTS_SCHEMA,
    database_properties_from_sections,
    extract_url,
    match_select,
    parse_kickoff_date,
    sections_to_blocks,
    split_person_names,
)
from designops.pipelines.intake import (
    build_user_message,
    parse_spreadsheet,
    render_preview_html,
    validate_sections_json,
)


SAMPLE_SECTIONS = {
    "title": "Acme — Club Portal",
    "properties_callout": {
        "status": "Intake",
        "scope": "Custom",
        "ux_ui_designer": "⚠ unassigned",
        "ba_dm_lead": "Jane",
        "kam": "Bob",
        "kickoff": "to confirm",
        "figma_drive_jira": "pending",
    },
    "section_1": {
        "website": "https://example.com",
        "paragraphs": ["Product overview.", "Why now."],
        "job_callout": "Our job right now (next ~4 weeks): UX discovery",
    },
    "section_2": {
        "personas": [{"title": "Member", "description": "Uses the portal daily."}],
        "not_for": "Not for admin staff.",
    },
    "section_3": {
        "phase_hours": "40 hours",
        "screens": [{"screen": "Login", "special": "SSO flow"}],
        "reuse": ["Header from design system"],
        "out_of_scope": ["Mobile app"],
        "revision_callout": "Two revision rounds included.",
    },
    "section_4": {
        "rows": [{"question": "Which auth?", "blocks": "Login", "who": "Client PM"}],
        "closing": "These questions are what this phase resolves.",
    },
    "section_5": {"rows": [{"who": "Jane (DM)", "ask_about": "Scope boundaries"}]},
    "section_6": {"bullets": ["Shared release calendar → handoff dates are real dates."]},
    "section_7": {
        "rows": [
            {"what": "Estimate", "status": "Linked", "where": "https://sheet.example.com"},
            {"what": "Brand assets", "status": "Missing", "where": "Requested"},
        ]
    },
    "section_8": {
        "project_links": [
            {"label": "🎨 Figma file", "url": "paste link here"},
            {"label": "🎫 Jira epic", "url": "paste link here"},
        ],
        "templates": [{"name": "Login screen", "status": "Not started"}],
        "sync_callout": "Statuses update automatically.",
    },
    "section_9": {"assistant_link": "paste Team assistant link here"},
    "section_10": {"placeholder": "Case study placeholder."},
}


def test_build_user_message_includes_all_fields():
    msg = build_user_message(
        pasted_input="Hello handover",
        estimate_link="https://est.example.com",
        proposal_link="https://prop.example.com",
        estimate_rows="Login\t8h",
        uploaded_files=[{"filename": "est.xlsx", "extracted_text": "Row1\tCol2"}],
        corrections="KAM is Alice",
    )
    assert "EMAIL_CONTENT:" in msg
    assert "Hello handover" in msg
    assert "ESTIMATE_LINK:" in msg
    assert "UPLOADED_FILE (est.xlsx):" in msg
    assert "CORRECTIONS:" in msg
    assert "KAM is Alice" in msg


def test_sample_intake_has_required_fields():
    from designops.pipelines.intake import SAMPLE_INTAKE

    assert "C-pipe handover" in SAMPLE_INTAKE["pasted_input"]
    assert SAMPLE_INTAKE["estimate_link"].startswith("https://")
    assert SAMPLE_INTAKE["proposal_link"].startswith("https://")
    assert "Club landing page" in SAMPLE_INTAKE["estimate_rows"]
    assert "Ana Taylor" in SAMPLE_INTAKE["corrections"]


def test_validate_sections_json_error_field():
    assert validate_sections_json({"error": "Not a handover"}) == "Not a handover"


def test_validate_sections_json_missing_title():
    assert validate_sections_json({}) == "LLM output missing title"


def test_parse_spreadsheet_csv():
    content = b"Screen,Hours\nLogin,8\nHome,12"
    text = parse_spreadsheet("estimate.csv", content)
    assert "Login" in text
    assert "8" in text


def test_parse_spreadsheet_xlsx():
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Screen", "Hours"])
    ws.append(["Login", 8])
    buf = io.BytesIO()
    wb.save(buf)
    text = parse_spreadsheet("estimate.xlsx", buf.getvalue())
    assert "Login" in text
    assert "8" in text


def test_parse_spreadsheet_unsupported():
    with pytest.raises(ValueError, match="Unsupported"):
        parse_spreadsheet("data.pdf", b"binary")


def test_sections_to_blocks_structure():
    blocks = sections_to_blocks(SAMPLE_SECTIONS)
    assert len(blocks) >= 10
    types = [b["type"] for b in blocks]
    assert "heading_2" in types
    assert "callout" in types
    assert "table" in types


def test_sections_to_blocks_includes_properties_callout():
    blocks = sections_to_blocks(SAMPLE_SECTIONS)
    first = blocks[0]
    assert first["type"] == "callout"
    assert "Intake" in first["callout"]["rich_text"][0]["text"]["content"]


def test_render_preview_html_contains_sections():
    html = render_preview_html(SAMPLE_SECTIONS)
    assert "1. What this project is" in html
    assert "Intake report" not in html
    assert "Club Portal" not in html  # title is not in preview body
    assert "Login" in html


def test_render_preview_html_error():
    html = render_preview_html({"error": "Not a handover email"})
    assert "Not a handover email" in html


def test_sections_to_blocks_skips_callout_for_database_row():
    blocks = sections_to_blocks(SAMPLE_SECTIONS, include_properties_callout=False)
    assert blocks[0]["type"] == "heading_2"
    assert "1. What this project is" in blocks[0]["heading_2"]["rich_text"][0]["text"]["content"]


def test_extract_url_ignores_placeholders():
    assert extract_url("pending") is None
    assert extract_url("paste link here") is None
    assert extract_url("https://www.figma.com/design/abc") == "https://www.figma.com/design/abc"


def test_parse_kickoff_date():
    assert parse_kickoff_date("to confirm") is None
    assert parse_kickoff_date("2026-08-15") == "2026-08-15"
    assert parse_kickoff_date("March 1, 2023") == "2023-03-01"


def test_split_person_names_skips_unassigned():
    assert split_person_names("⚠ unassigned") == []
    assert split_person_names("Olga Kimalana, Iryna Rubanava") == ["Olga Kimalana", "Iryna Rubanava"]


def test_status_intake_maps_to_todo():
    options = ["To Do", "Done", "Canceled"]
    assert match_select("Intake", options, {"intake": "To Do"}) == "To Do"
    assert match_select("Done", options, {"intake": "To Do"}) == "Done"


def test_database_properties_maps_design_projects_row():
    sections = {
        "title": "Sports Group Denmark - Club Portal",
        "properties_callout": {
            "status": "Intake",
            "scope": "Core",
            "ba_dm_lead": "Olga Kimalana",
            "kam": "Ana Taylor",
            "ux_ui_designer": "⚠ unassigned",
            "kickoff": "to confirm",
        },
        "database_properties": {
            "name": "Sports Group Denmark - Club Portal",
            "status": "To Do",
            "scope": "Core",
            "site_type": ["B2B"],
            "ba_dm_lead": ["Olga Kimalana"],
            "kam": ["Ana Taylor"],
            "ux_ui_designer": [],
            "date": None,
            "figma": None,
        },
    }
    users = [
        {"id": "u-olga", "name": "Olga Kimalana"},
        {"id": "u-ana", "name": "Ana Taylor"},
    ]
    props = database_properties_from_sections(
        sections,
        title="Sports Group Denmark - Club Portal",
        schema=DESIGN_PROJECTS_SCHEMA,
        users=users,
    )
    assert props["Name"]["title"][0]["text"]["content"] == "Sports Group Denmark - Club Portal"
    assert props["Status"]["select"]["name"] == "To Do"
    assert props["Scope"]["select"]["name"] == "Core"
    assert props["Site type"]["multi_select"] == [{"name": "B2B"}]
    assert props["BA/DM lead"]["people"] == [{"id": "u-olga"}]
    assert props["KAM"]["people"] == [{"id": "u-ana"}]
    assert "UX/UI designer" not in props
    assert "Date" not in props
    assert "FIGMA" not in props


def test_database_properties_sets_real_urls_only():
    sections = {
        "title": "IONTO",
        "database_properties": {
            "figma": "https://www.figma.com/design/abc",
            "jira_phase_1": "pending",
        },
    }
    props = database_properties_from_sections(sections, title="IONTO")
    assert props["FIGMA"]["url"] == "https://www.figma.com/design/abc"
    assert "JIRA: Phase 1" not in props


def test_db_props_harvests_section_links():
    from designops.adapters.notion import _db_props_source

    src = _db_props_source(
        {
            "database_properties": {"figma": None, "design_contract": None},
            "section_7": {
                "rows": [
                    {
                        "what": "Proposal",
                        "where": "https://docs.google.com/presentation/d/abc",
                        "status": "Linked",
                    }
                ]
            },
            "section_8": {
                "project_links": [
                    {"label": "🎨 Figma file", "url": "https://www.figma.com/design/xyz"},
                    {"label": "Jira epic", "url": "paste link here"},
                ]
            },
        }
    )
    assert src["figma"] == "https://www.figma.com/design/xyz"
    assert src["design_contract"] == "https://docs.google.com/presentation/d/abc"


def test_unmatched_people_notes_when_no_users():
    from designops.adapters.notion import unmatched_people_notes

    notes = unmatched_people_notes(
        {"database_properties": {"ba_dm_lead": ["Iryna Rubanava"], "kam": ["Ana Taylor"]}},
        users=[],
    )
    assert notes == ["BA/DM lead: Iryna Rubanava", "KAM: Ana Taylor"]
