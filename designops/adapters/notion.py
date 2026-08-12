"""Notion API adapter — create design intake pages from structured JSON.

Maps sections_json (from LLM) to Notion block types and creates pages under a
parent page or in a database.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from designops.core.config import Settings, get_settings

log = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
# Notion API allows max 100 children per request; batch appends above that.
_BLOCK_BATCH = 100

_STATUS_COLORS = {
    "linked": "green",
    "link it": "orange",
    "missing": "red",
    "pending": "gray",
    "not started": "gray",
}


def _rt(text: str, *, bold: bool = False, link: str | None = None) -> dict:
    """Single rich_text segment."""
    obj: dict[str, Any] = {"type": "text", "text": {"content": text[:2000]}}
    if link:
        obj["text"]["link"] = {"url": link}
    ann: dict[str, bool] = {}
    if bold:
        ann["bold"] = True
    if ann:
        obj["annotations"] = ann
    return obj


def _rich(text: str, *, bold: bool = False, link: str | None = None) -> list[dict]:
    if not text:
        return [_rt(" ")]
    return [_rt(str(text), bold=bold, link=link)]


def _heading2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _rich(text)},
    }


def _paragraph(text: str, *, bold: bool = False) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich(text, bold=bold)},
    }


def _bullets(items: list[str]) -> list[dict]:
    return [
        {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(item)},
        }
        for item in items
        if item
    ]


def _callout(text: str, *, emoji: str = "📋", color: str = "default") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rich(text),
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def _table(headers: list[str], rows: list[list[str]]) -> dict:
    """Build a table block with table_row children."""
    width = len(headers)
    cells = [_rich(h, bold=True) for h in headers]
    children: list[dict] = [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        }
    ]
    for row in rows:
        padded = (row + [""] * width)[:width]
        children.append(
            {
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": [_rich(c) for c in padded]},
            }
        )
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def _persona_columns(personas: list[dict]) -> dict | None:
    if not personas:
        return None
    cols = []
    for p in personas[:2]:
        title = p.get("title") or "Persona"
        desc = p.get("description") or ""
        cols.append(
            {
                "object": "block",
                "type": "column",
                "column": {
                    "children": [
                        _callout(f"{title}\n{desc}", emoji="👤", color="blue_background"),
                    ]
                },
            }
        )
    if len(cols) == 1:
        cols.append(
            {
                "object": "block",
                "type": "column",
                "column": {"children": [_paragraph("")]},
            }
        )
    return {
        "object": "block",
        "type": "column_list",
        "column_list": {"children": cols},
    }


def _normalize_id(page_id: str) -> str:
    """Strip dashes/spaces from Notion IDs."""
    return re.sub(r"[^a-fA-F0-9]", "", page_id)


def _status_color(status: str) -> str:
    return _STATUS_COLORS.get((status or "").strip().lower(), "default")


def sections_to_blocks(sections: dict) -> list[dict]:
    """Convert sections_json to Notion API block objects."""
    blocks: list[dict] = []

    props = sections.get("properties_callout") or {}
    if props:
        parts = [
            f"Status: {props.get('status', 'Intake')}",
            f"Scope: {props.get('scope', '')}",
            f"UX/UI designer: {props.get('ux_ui_designer', '')}",
            f"BA/DM lead: {props.get('ba_dm_lead', '')}",
            f"KAM: {props.get('kam', '')}",
            f"Kickoff: {props.get('kickoff', '')}",
            f"Figma/Drive/Jira: {props.get('figma_drive_jira', 'pending')}",
        ]
        blocks.append(_callout(" · ".join(p for p in parts if p), emoji="📋"))

    s1 = sections.get("section_1") or {}
    if s1:
        blocks.append(_heading2("1. What this project is"))
        if s1.get("website"):
            blocks.append(_paragraph(f"Website: {s1['website']}"))
        for para in s1.get("paragraphs") or []:
            blocks.append(_paragraph(para))
        if s1.get("job_callout"):
            blocks.append(_callout(s1["job_callout"], emoji="🎯", color="blue_background"))

    s2 = sections.get("section_2") or {}
    if s2:
        blocks.append(_heading2("2. Who uses it"))
        col = _persona_columns(s2.get("personas") or [])
        if col:
            blocks.append(col)
        if s2.get("not_for"):
            blocks.append(_paragraph(s2["not_for"]))

    s3 = sections.get("section_3") or {}
    if s3:
        hours = s3.get("phase_hours")
        heading = "3. What we design"
        if hours:
            heading += f" ({hours})"
        blocks.append(_heading2(heading))
        screens = s3.get("screens") or []
        if screens:
            blocks.append(_paragraph("✏️ New screens", bold=True))
            blocks.append(
                _table(
                    ["Screen", "What's special about it"],
                    [[r.get("screen", ""), r.get("special", "")] for r in screens],
                )
            )
        reuse = s3.get("reuse") or []
        if reuse:
            blocks.append(_paragraph("♻️ Reuse", bold=True))
            blocks.extend(_bullets(reuse))
        oos = s3.get("out_of_scope") or []
        if oos:
            blocks.append(_paragraph("🚫 Out of scope", bold=True))
            blocks.extend(_bullets(oos))
        if s3.get("revision_callout"):
            blocks.append(_callout(s3["revision_callout"], emoji="✂️", color="yellow_background"))

    s4 = sections.get("section_4") or {}
    if s4:
        blocks.append(_heading2("4. Open questions — and which screens they block"))
        rows = s4.get("rows") or []
        if rows:
            blocks.append(
                _table(
                    ["Open question", "Blocks", "Who has the answer"],
                    [[r.get("question", ""), r.get("blocks", ""), r.get("who", "")] for r in rows],
                )
            )
        if s4.get("closing"):
            blocks.append(_paragraph(s4["closing"]))

    s5 = sections.get("section_5") or {}
    if s5:
        blocks.append(_heading2("5. People"))
        rows = s5.get("rows") or []
        if rows:
            blocks.append(
                _table(
                    ["Who", "Ask them about"],
                    [[r.get("who", ""), r.get("ask_about", "")] for r in rows],
                )
            )

    s6 = sections.get("section_6") or {}
    if s6:
        blocks.append(_heading2("6. Heads-up"))
        blocks.extend(_bullets(s6.get("bullets") or []))

    s7 = sections.get("section_7") or {}
    if s7:
        blocks.append(_heading2("7. Files & links"))
        rows = s7.get("rows") or []
        if rows:
            blocks.append(
                _table(
                    ["What", "Status", "Where"],
                    [[r.get("what", ""), r.get("status", ""), r.get("where", "")] for r in rows],
                )
            )

    s8 = sections.get("section_8") or {}
    if s8:
        blocks.append(_heading2("8. Working space"))
        blocks.append(_paragraph("1️⃣ Kick off meeting", bold=True))
        blocks.append(_paragraph("2️⃣ UX discovery", bold=True))
        blocks.append(_paragraph("3️⃣ UX/UI designs", bold=True))
        plinks = s8.get("project_links") or []
        if plinks:
            blocks.append(_paragraph("Project links", bold=True))
            blocks.append(
                _table(
                    ["Link", "URL"],
                    [[r.get("label", ""), r.get("url", "paste link here")] for r in plinks],
                )
            )
        templates = s8.get("templates") or []
        if templates:
            blocks.append(_paragraph("Templates to produce", bold=True))
            blocks.append(
                _table(
                    ["Template", "Status"],
                    [[r.get("name", ""), r.get("status", "Not started")] for r in templates],
                )
            )
        if s8.get("sync_callout"):
            blocks.append(_callout(s8["sync_callout"], emoji="🔄", color="gray_background"))

    s9 = sections.get("section_9") or {}
    if s9:
        blocks.append(_heading2("9. Project AI assistant"))
        link = s9.get("assistant_link") or "paste Team assistant link here"
        blocks.append(_paragraph(f"🔗 Team assistant link: {link}"))

    s10 = sections.get("section_10") or {}
    if s10:
        blocks.append(_heading2("10. Case study"))
        blocks.append(_paragraph(s10.get("placeholder") or "Case study placeholder."))

    return blocks


def _page_url(page_id: str) -> str:
    pid = _normalize_id(page_id)
    return f"https://www.notion.so/{pid}"


class NotionClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._client = None

    @property
    def configured(self) -> bool:
        return self.s.notion_configured

    def _get_client(self):
        if self._client is None:
            if not self.s.notion_api_token:
                raise RuntimeError("NOTION_API_TOKEN not set")
            from notion_client import Client

            self._client = Client(auth=self.s.notion_api_token, notion_version=NOTION_VERSION)
        return self._client

    def test_connection(self) -> dict:
        """Verify token works. Returns bot user info."""
        return self._get_client().users.me()

    def create_intake_page(
        self,
        *,
        title: str,
        sections_json: dict,
        parent_id: str | None = None,
        is_database: bool | None = None,
    ) -> tuple[str, str]:
        """Create intake page in Notion. Returns (page_id, page_url)."""
        parent_id = parent_id or self.s.notion_parent_page_id
        if not parent_id:
            raise RuntimeError("NOTION_PARENT_PAGE_ID not set")
        parent_id = _normalize_id(parent_id)
        is_db = is_database if is_database is not None else self.s.notion_parent_is_database

        blocks = sections_to_blocks(sections_json)
        if not blocks:
            raise ValueError("No blocks to publish — sections_json is empty")

        client = self._get_client()

        if is_db:
            props = sections_json.get("properties_callout") or {}
            properties: dict[str, Any] = {
                "Name": {"title": [{"text": {"content": title[:2000]}}]},
            }
            if props.get("status"):
                properties["Status"] = {"select": {"name": props["status"]}}
            if props.get("scope"):
                properties["Scope"] = {"select": {"name": props["scope"]}}
            parent = {"database_id": parent_id}
            create_kwargs: dict[str, Any] = {
                "parent": parent,
                "properties": properties,
                "children": blocks[:_BLOCK_BATCH],
            }
        else:
            parent = {"page_id": parent_id}
            create_kwargs = {
                "parent": parent,
                "properties": {
                    "title": [{"text": {"content": title[:2000]}}],
                },
                "children": blocks[:_BLOCK_BATCH],
            }

        page = client.pages.create(**create_kwargs)
        page_id = page["id"]

        # Append remaining blocks in batches
        remaining = blocks[_BLOCK_BATCH:]
        while remaining:
            batch = remaining[:_BLOCK_BATCH]
            remaining = remaining[_BLOCK_BATCH:]
            client.blocks.children.append(block_id=page_id, children=batch)

        url = page.get("url") or _page_url(page_id)
        log.info("Created Notion intake page id=%s url=%s", page_id, url)
        return page_id, url
