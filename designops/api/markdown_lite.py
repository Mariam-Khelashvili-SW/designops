"""Minimal markdown → HTML for call-summary email drafts.

Drafts are plain text with ``**section labels**`` and ``- `` / ``* `` bullets (no HTML).
This renderer escapes first, then applies a small safe subset with inline styles so
Copy body pastes into Gmail/Outlook with bold and bullet formatting intact.
"""

from __future__ import annotations

import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# Inline styles — email clients strip class/CSS; paste needs these on elements.
_P = (
    'margin:0 0 12px 0;font-family:-apple-system,BlinkMacSystemFont,'
    '"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;'
    "line-height:1.55;color:#1f2328"
)
_STRONG = "font-weight:700"
_UL = "margin:4px 0 14px 0;padding-left:22px;list-style-type:disc"
_LI = "margin:4px 0;padding-left:2px"
_A = "color:#1d5fb0;text-decoration:underline"


def _strip_inline_markers(text: str) -> str:
    out = _BOLD_RE.sub(r"\1", text)
    return _MD_LINK_RE.sub(r"\1 (\2)", out)


def _inline(text: str) -> str:
    """Escape, then bold + markdown links with inline styles."""
    parts: list[str] = []
    last = 0
    for m in _MD_LINK_RE.finditer(text):
        parts.append(html.escape(text[last : m.start()]))
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        parts.append(f'<a href="{url}" style="{_A}" rel="noopener noreferrer">{label}</a>')
        last = m.end()
    parts.append(html.escape(text[last:]))
    escaped = "".join(parts)
    return _BOLD_RE.sub(
        rf'<strong style="{_STRONG}">\1</strong>',
        escaped,
    )


def _bullet_line(stripped: str) -> str | None:
    for prefix in ("- ", "* ", "• "):
        if stripped.startswith(prefix):
            return stripped[len(prefix) :]
    return None


def markdown_lite_to_html(text: str) -> str:
    """Convert draft body markdown-lite to safe, paste-friendly HTML."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        inner = "".join(f'<li style="{_LI}">{item}</li>' for item in list_items)
        blocks.append(f'<ul style="{_UL}">{inner}</ul>')
        list_items = []

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            flush_list()
            continue
        bullet = _bullet_line(stripped)
        if bullet is not None:
            list_items.append(_inline(bullet))
            continue
        flush_list()
        blocks.append(f'<p style="{_P}">{_inline(stripped)}</p>')

    flush_list()
    return "\n".join(blocks)


def markdown_lite_to_plain(text: str) -> str:
    """Plain text with bullets and bold markers removed (for text/plain clipboard)."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        bullet = _bullet_line(stripped)
        if bullet is not None:
            out.append("• " + _strip_inline_markers(bullet))
            continue
        out.append(_strip_inline_markers(stripped))

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
