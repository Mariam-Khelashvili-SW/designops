"""Unit tests for design intake pipeline (no Anthropic / no Notion / no DB)."""

from __future__ import annotations

import io

import pytest

from designops.adapters.notion import sections_to_blocks
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


def test_validate_sections_json_ok():
    assert validate_sections_json(SAMPLE_SECTIONS) is None


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
