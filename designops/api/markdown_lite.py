"""Minimal markdown → HTML for call-summary email drafts.

Drafts are plain text with ``**section labels**`` and ``- `` bullets (no HTML).
This renderer escapes first, then applies a small safe subset.
"""

from __future__ import annotations

import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text: str) -> str:
    """Escape, then bold + markdown links."""
    parts: list[str] = []
    last = 0
    # Protect markdown links first on raw text
    for m in _MD_LINK_RE.finditer(text):
        parts.append(html.escape(text[last : m.start()]))
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        parts.append(f'<a href="{url}" rel="noopener noreferrer">{label}</a>')
        last = m.end()
    parts.append(html.escape(text[last:]))
    escaped = "".join(parts)
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def markdown_lite_to_html(text: str) -> str:
    """Convert draft body markdown-lite to safe HTML."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        inner = "".join(f"<li>{item}</li>" for item in list_items)
        blocks.append(f"<ul>{inner}</ul>")
        list_items = []

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            flush_list()
            continue
        if stripped.startswith("- "):
            list_items.append(_inline(stripped[2:]))
            continue
        flush_list()
        blocks.append(f"<p>{_inline(stripped)}</p>")

    flush_list()
    return "\n".join(blocks)


def markdown_lite_to_plain(text: str) -> str:
    """Plain text without ``**`` markers (for text/plain clipboard)."""
    if not text:
        return ""
    out = _BOLD_RE.sub(r"\1", text)
    out = _MD_LINK_RE.sub(r"\1 (\2)", out)
    return out
