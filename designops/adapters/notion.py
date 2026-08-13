"""Notion API adapter — create design intake pages from structured JSON.

Maps sections_json (from LLM) to Notion block types and creates pages under a
parent page or in a database.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from designops.core.config import Settings, get_settings

log = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"
# Notion API allows max 100 children per request; batch appends above that.
_BLOCK_BATCH = 100

_STATUS_COLORS = {
    "linked": "green",
    "link it": "orange",
    "missing": "red",
    "pending": "gray",
    "not started": "gray",
}

# Live Design projects DB (7403ff7d…) — used when schema retrieve fails.
DESIGN_PROJECTS_SCHEMA: dict[str, dict] = {
    "Name": {"type": "title"},
    "BA/DM lead": {"type": "people"},
    "Date": {"type": "date"},
    "Design Drive": {"type": "url"},
    "Design contract": {"type": "url"},
    "FIGMA": {"type": "url"},
    "JIRA Phase 2": {"type": "url"},
    "JIRA: Phase 1": {"type": "url"},
    "JIRA: UX/UI Designs": {"type": "url"},
    "KAM": {"type": "people"},
    "Scope": {
        "type": "select",
        "select": {
            "options": [
                {"name": "Core"},
                {"name": "Full"},
                {"name": "Custom"},
                {"name": "UI Refresh"},
                {"name": "Improved UX & UI Refresh"},
                {"name": "Webflow"},
                {"name": "Partial improvements"},
                {"name": "Agile"},
            ]
        },
    },
    "Site type": {
        "type": "multi_select",
        "multi_select": {
            "options": [
                {"name": "B2B"},
                {"name": "B2C"},
                {"name": "D2C"},
                {"name": "B2G"},
                {"name": "Custom"},
            ]
        },
    },
    "Status": {
        "type": "select",
        "select": {
            "options": [
                {"name": "To Do"},
                {"name": "Research in progress"},
                {"name": "Wireframes in progress"},
                {"name": "Designs in progress"},
                {"name": "Dedicated"},
                {"name": "On hold"},
                {"name": "Done"},
                {"name": "Canceled"},
            ]
        },
    },
    "UX/UI designer": {"type": "people"},
}

SCOPE_ALIASES = {
    "template": "Custom",
    "ui refresh": "UI Refresh",
    "improved ux": "Improved UX & UI Refresh",
    "improved ux & ui refresh": "Improved UX & UI Refresh",
    "partial": "Partial improvements",
    "partial improvements": "Partial improvements",
}

# "Intake" is not a live Status option yet (Olga sign-off). New rows start as To Do.
STATUS_ALIASES = {
    "intake": "To Do",
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


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


def extract_url(text: str | None) -> str | None:
    """Return a real http(s) URL, or None for pending/placeholder text."""
    if not text:
        return None
    t = str(text).strip()
    low = t.lower()
    if low in (
        "pending",
        "n/a",
        "na",
        "to confirm",
        "paste link here",
        "paste link here so the whole team uses the same one",
    ):
        return None
    m = _URL_RE.search(t)
    if not m:
        return None
    return m.group(0).rstrip(").,;")


def parse_kickoff_date(text: str | None) -> str | None:
    """Parse a kickoff string to YYYY-MM-DD, or None if unconfirmed."""
    if not text:
        return None
    t = str(text).strip()
    if not t:
        return None
    low = t.lower()
    if "confirm" in low or low in ("pending", "unknown", "n/a", "na"):
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def split_person_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(split_person_names(item))
        return names
    s = str(value).strip()
    if not s or s.startswith("⚠") or "unassigned" in s.lower() or "not assigned" in s.lower():
        return []
    return [p.strip() for p in re.split(r",|;|/|\band\b", s) if p.strip()]


def match_select(
    value: str | None,
    options: list[str],
    aliases: dict[str, str] | None = None,
) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith("⚠"):
        return None
    opts_lower = {o.lower(): o for o in options}
    if raw.lower() in opts_lower:
        return opts_lower[raw.lower()]
    if aliases:
        mapped = aliases.get(raw.lower())
        if mapped and mapped.lower() in opts_lower:
            return opts_lower[mapped.lower()]
    return None


def match_user_ids(names: list[str], users: list[dict]) -> list[str]:
    """Match display names to Notion user ids. Skip if zero or many hits."""
    ids: list[str] = []
    for name in names:
        needle = name.lower().strip()
        if not needle:
            continue
        exact = [u for u in users if (u.get("name") or "").lower() == needle]
        if len(exact) == 1:
            ids.append(exact[0]["id"])
            continue
        partial = [
            u
            for u in users
            if needle in (u.get("name") or "").lower() or (u.get("name") or "").lower() in needle
        ]
        if len(partial) == 1:
            ids.append(partial[0]["id"])
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _schema_options(prop: dict) -> list[str]:
    ptype = prop.get("type")
    nested = prop.get(ptype) or {}
    return [o.get("name") for o in (nested.get("options") or []) if o.get("name")]


def _find_prop(schema: dict, *candidates: str) -> tuple[str | None, dict | None]:
    for cand in candidates:
        for key, val in schema.items():
            if key.lower() == cand.lower():
                return key, val
    return None, None


def _db_props_source(sections: dict) -> dict:
    src = dict(sections.get("properties_callout") or {})
    src.update(sections.get("database_properties") or {})

    # Harvest real URLs from Files & links / Working space when DB fields are empty.
    if not extract_url(src.get("figma") or src.get("figma_url")):
        for link in (sections.get("section_8") or {}).get("project_links") or []:
            label = str((link or {}).get("label") or "").lower()
            url = extract_url((link or {}).get("url"))
            if url and "figma" in label:
                src["figma"] = url
                break
    if not extract_url(src.get("jira_ux_ui") or src.get("jira")):
        for link in (sections.get("section_8") or {}).get("project_links") or []:
            label = str((link or {}).get("label") or "").lower()
            url = extract_url((link or {}).get("url"))
            if url and "jira" in label:
                src["jira_ux_ui"] = url
                break
    if not extract_url(src.get("design_contract")):
        for row in (sections.get("section_7") or {}).get("rows") or []:
            what = str((row or {}).get("what") or "").lower()
            url = extract_url((row or {}).get("where"))
            if url and ("proposal" in what or "contract" in what):
                src["design_contract"] = url
                break
    if not extract_url(src.get("design_drive")):
        for row in (sections.get("section_7") or {}).get("rows") or []:
            what = str((row or {}).get("what") or "").lower()
            url = extract_url((row or {}).get("where"))
            if url and ("drive" in what or "folder" in what):
                src["design_drive"] = url
                break
    return src


def unmatched_people_notes(sections: dict, *, users: list[dict] | None = None) -> list[str]:
    """People names we could not map to Notion user ids (column stays empty)."""
    users = users or []
    src = _db_props_source(sections)
    notes: list[str] = []
    for label, raw in (
        ("BA/DM lead", src.get("ba_dm_lead")),
        ("KAM", src.get("kam")),
        ("UX/UI designer", src.get("ux_ui_designer")),
    ):
        names = split_person_names(raw)
        if not names:
            continue
        if not match_user_ids(names, users):
            notes.append(f"{label}: {', '.join(names)}")
    return notes


def database_properties_from_sections(
    sections: dict,
    *,
    title: str,
    schema: dict | None = None,
    users: list[dict] | None = None,
) -> dict[str, Any]:
    """Map intake JSON onto Design projects row properties. Skip unknowns."""
    schema = schema or DESIGN_PROJECTS_SCHEMA
    users = users or []
    src = _db_props_source(sections)
    properties: dict[str, Any] = {}

    name_key, name_prop = _find_prop(schema, "Name", "title")
    row_name = (src.get("name") or title or sections.get("title") or "Untitled intake").strip()
    if name_key and name_prop and name_prop.get("type") == "title":
        properties[name_key] = {"title": [{"text": {"content": row_name[:2000]}}]}

    status_key, status_prop = _find_prop(schema, "Status")
    if status_key and status_prop and status_prop.get("type") == "select":
        options = _schema_options(status_prop)
        mapped = match_select(src.get("status"), options, STATUS_ALIASES)
        if not mapped and "to do" in {o.lower() for o in options}:
            mapped = next(o for o in options if o.lower() == "to do")
        if mapped:
            properties[status_key] = {"select": {"name": mapped}}

    scope_key, scope_prop = _find_prop(schema, "Scope")
    if scope_key and scope_prop and scope_prop.get("type") == "select":
        mapped = match_select(src.get("scope"), _schema_options(scope_prop), SCOPE_ALIASES)
        if mapped:
            properties[scope_key] = {"select": {"name": mapped}}

    site_key, site_prop = _find_prop(schema, "Site type")
    if site_key and site_prop and site_prop.get("type") == "multi_select":
        options = _schema_options(site_prop)
        raw = src.get("site_type") or src.get("site_types") or []
        if isinstance(raw, str):
            raw = [p.strip() for p in re.split(r"[,/]", raw) if p.strip()]
        selected = []
        for item in raw:
            hit = match_select(str(item), options)
            if hit and hit not in selected:
                selected.append(hit)
        if selected:
            properties[site_key] = {"multi_select": [{"name": n} for n in selected]}

    date_key, date_prop = _find_prop(schema, "Date")
    if date_key and date_prop and date_prop.get("type") == "date":
        iso = parse_kickoff_date(src.get("date") or src.get("kickoff"))
        if iso:
            properties[date_key] = {"date": {"start": iso}}

    people_map = {
        "BA/DM lead": src.get("ba_dm_lead"),
        "KAM": src.get("kam"),
        "UX/UI designer": src.get("ux_ui_designer"),
    }
    for label, raw_names in people_map.items():
        key, prop = _find_prop(schema, label)
        if not key or not prop or prop.get("type") != "people":
            continue
        ids = match_user_ids(split_person_names(raw_names), users)
        if ids:
            properties[key] = {"people": [{"id": i} for i in ids]}

    url_map = {
        "FIGMA": src.get("figma") or src.get("figma_url"),
        "Design Drive": src.get("design_drive"),
        "Design contract": src.get("design_contract"),
        "JIRA Phase 2": src.get("jira_phase_2"),
        "JIRA: Phase 1": src.get("jira_phase_1"),
        "JIRA: UX/UI Designs": src.get("jira_ux_ui") or src.get("jira"),
    }
    for label, raw in url_map.items():
        key, prop = _find_prop(schema, label)
        if not key or not prop or prop.get("type") != "url":
            continue
        url = extract_url(raw)
        if url:
            properties[key] = {"url": url}

    return properties


def sections_to_blocks(sections: dict, *, include_properties_callout: bool = True) -> list[dict]:
    """Convert sections_json to Notion API block objects.

    On a Design projects database row the properties live on the table, so the
    sample-page 📋 callout is omitted (see SGD sample note).
    """
    blocks: list[dict] = []

    props = sections.get("properties_callout") or {}
    if include_properties_callout and props:
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
            blocks.append(_paragraph("Project links (one per project — paste once at design kickoff)", bold=True))
            blocks.append(
                _table(
                    ["Link", "Where"],
                    [[r.get("label", ""), r.get("url") or r.get("where") or "paste link here"] for r in plinks],
                )
            )
        templates = s8.get("templates") or []
        if templates:
            blocks.append(_paragraph("Templates to produce — Wireframes", bold=True))
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

    def _parent_is_database(self, parent_id: str) -> bool:
        try:
            self._get_client().databases.retrieve(parent_id)
            return True
        except Exception as e:  # noqa: BLE001 — 404 if it's a page
            msg = str(e).lower()
            if "is a page" in msg:
                return False
            return False

    def _find_child_database(self, parent_id: str) -> str | None:
        """Return the first inline/child database on a page, if any."""
        client = self._get_client()
        cursor = None
        while True:
            kwargs: dict[str, Any] = {"page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = client.blocks.children.list(parent_id, **kwargs)
            for b in resp.get("results") or []:
                if b.get("type") == "child_database" and not b.get("in_trash"):
                    return b.get("id")
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return None

    def _projects_database_id(self, parent_id: str) -> str | None:
        """Database to write rows into: the parent itself, or an inline table on it."""
        if self.s.notion_parent_is_database:
            return parent_id
        child = self._find_child_database(parent_id)
        if child:
            return child
        if self._parent_is_database(parent_id):
            return parent_id
        return None

    def _database_and_schema(self, database_id: str) -> tuple[str, dict]:
        """Return (page parent id for create, property schema).

        Notion 2025: rows belong to a data_source, not the database container.
        """
        client = self._get_client()
        try:
            db = client.databases.retrieve(database_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not retrieve Notion database: %s", e)
            return database_id, DESIGN_PROJECTS_SCHEMA

        sources = db.get("data_sources") or []
        if sources:
            ds_id = sources[0]["id"]
            try:
                src = client.data_sources.retrieve(ds_id)
                schema = src.get("properties") or db.get("properties") or DESIGN_PROJECTS_SCHEMA
            except Exception as e:  # noqa: BLE001
                log.warning("Could not retrieve Notion data source: %s", e)
                schema = db.get("properties") or DESIGN_PROJECTS_SCHEMA
            return ds_id, schema
        return database_id, db.get("properties") or DESIGN_PROJECTS_SCHEMA

    def _retrieve_schema(self, database_id: str) -> dict:
        _, schema = self._database_and_schema(database_id)
        return schema

    def _list_workspace_users(self) -> list[dict]:
        users: list[dict] = []
        cursor = None
        try:
            client = self._get_client()
            while True:
                kwargs: dict[str, Any] = {"page_size": 100}
                if cursor:
                    kwargs["start_cursor"] = cursor
                resp = client.users.list(**kwargs)
                for u in resp.get("results") or []:
                    if u.get("type") == "person" and u.get("name"):
                        users.append({"id": u["id"], "name": u["name"]})
                if not resp.get("has_more"):
                    break
                cursor = resp.get("next_cursor")
        except Exception as e:  # noqa: BLE001
            log.warning("Could not list Notion users (people fields will be empty): %s", e)
        return users

    def create_intake_page(
        self,
        *,
        title: str,
        sections_json: dict,
        parent_id: str | None = None,
        is_database: bool | None = None,
    ) -> tuple[str, str]:
        """Create an intake page under the configured parent. Returns (page_id, page_url)."""
        parent_id = parent_id or self.s.notion_parent_page_id
        if not parent_id:
            raise RuntimeError("NOTION_PARENT_PAGE_ID not set")
        parent_id = _normalize_id(parent_id)
        client = self._get_client()

        db_id = self._projects_database_id(parent_id)
        if is_database is None:
            is_db = bool(db_id)
        else:
            is_db = is_database
            if is_db and not db_id:
                db_id = parent_id

        blocks = sections_to_blocks(
            sections_json, include_properties_callout=not is_db
        )
        if not blocks:
            raise ValueError("No blocks to publish — sections_json is empty")

        if is_db:
            row_parent_id, schema = self._database_and_schema(db_id or parent_id)
            users = self._list_workspace_users()
            properties = database_properties_from_sections(
                sections_json, title=title, schema=schema, users=users
            )
            if "Name" not in properties and not any(
                (schema.get(k) or {}).get("type") == "title" and k in properties
                for k in properties
            ):
                properties["Name"] = {"title": [{"text": {"content": title[:2000]}}]}
            # People columns need Notion user ids. This token often cannot list users,
            # so keep the names visible in the page body when columns stay empty.
            people_notes = unmatched_people_notes(sections_json, users=users)
            if people_notes:
                blocks = [
                    _callout(
                        "People (set manually in the table — integration cannot resolve Notion users): "
                        + " · ".join(people_notes),
                        emoji="👤",
                        color="gray_background",
                    ),
                    *blocks,
                ]
            # 2025 API: parent is the data source. Older DBs still accept database_id.
            target_db = db_id or parent_id
            sources_exist = row_parent_id != target_db
            parent_obj = (
                {"type": "data_source_id", "data_source_id": row_parent_id}
                if sources_exist
                else {"database_id": target_db}
            )
            create_kwargs: dict[str, Any] = {
                "parent": parent_obj,
                "properties": properties,
                "children": blocks[:_BLOCK_BATCH],
            }
        else:
            create_kwargs = {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": [{"text": {"content": title[:2000]}}],
                },
                "children": blocks[:_BLOCK_BATCH],
            }

        page = client.pages.create(**create_kwargs)
        page_id = page["id"]

        remaining = blocks[_BLOCK_BATCH:]
        while remaining:
            batch = remaining[:_BLOCK_BATCH]
            remaining = remaining[_BLOCK_BATCH:]
            client.blocks.children.append(block_id=page_id, children=batch)

        url = page.get("url") or _page_url(page_id)
        log.info("Created Notion intake page id=%s url=%s db=%s", page_id, url, is_db)
        return page_id, url

    def list_intake_pages(self, parent_id: str | None = None) -> list[dict]:
        """List intake pages from the Notion parent (inline Design projects table + child pages)."""
        parent_id = _normalize_id(parent_id or self.s.notion_parent_page_id)
        if not parent_id:
            return []
        client = self._get_client()
        rows: list[dict] = []
        seen: set[str] = set()

        def _add(*, pid: str, name: str, url: str | None = None, source: str, edited: str | None = None):
            key = _normalize_id(pid)
            if not key or key in seen:
                return
            seen.add(key)
            rows.append(
                {
                    "id": pid,
                    "name": (name or "").strip() or "Untitled",
                    "url": url or _page_url(pid),
                    "source": source,
                    "last_edited_time": edited,
                }
            )

        db_id = self._projects_database_id(parent_id)
        if db_id:
            ds_id, _schema = self._database_and_schema(db_id)
            cursor = None
            while True:
                kwargs: dict[str, Any] = {"page_size": 100}
                if cursor:
                    kwargs["start_cursor"] = cursor
                resp = client.data_sources.query(ds_id, **kwargs)
                for page in resp.get("results") or []:
                    if page.get("in_trash") or page.get("archived"):
                        continue
                    name = ""
                    for v in (page.get("properties") or {}).values():
                        if v.get("type") == "title":
                            name = "".join(
                                x.get("plain_text", "") for x in (v.get("title") or [])
                            )
                            break
                    _add(
                        pid=page.get("id") or "",
                        name=name,
                        url=page.get("url"),
                        source="table",
                        edited=page.get("last_edited_time"),
                    )
                if not resp.get("has_more"):
                    break
                cursor = resp.get("next_cursor")

        cursor = None
        while True:
            kwargs = {"page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = client.blocks.children.list(parent_id, **kwargs)
            for b in resp.get("results") or []:
                if b.get("type") != "child_page" or b.get("in_trash") or b.get("archived"):
                    continue
                title = (b.get("child_page") or {}).get("title") or ""
                _add(
                    pid=b.get("id") or "",
                    name=title,
                    source="page",
                    edited=b.get("last_edited_time"),
                )
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows

