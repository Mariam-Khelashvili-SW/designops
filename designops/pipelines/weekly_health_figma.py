"""Figma comment queue for weekly health — thread rollup per spec (Aug 2026).

Reports only what is unanswered or unfinished on our side. Resolved threads are
skipped. Deterministic — no model calls.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from designops.core.config import Settings, get_settings
from designops.pipelines.weekly_health_math import working_days_between

_ROSTER_PATH = Path(__file__).resolve().parent.parent / "config" / "figma_roster.json"
_DEFAULT_MAX_FILES = 5
_OVERDUE_WORKING_DAYS = 5  # "more than a week" = strictly > 5 Mon–Fri days

_QUESTION_RE = re.compile(
    r"\?|"
    r"\bneed to (?:clarify|align|discuss)\b|"
    r"\bto clarify\b|"
    r"\bnot sure\b|"
    r"\bcan you\b|"
    r"\bis it possible\b|"
    r"\bcould you\b",
    re.IGNORECASE,
)
_IMPERATIVE_RE = re.compile(
    r"^(?:add|create|remove|delete|change|update|fix|make|check|adjust|align|"
    r"annotate|discuss|rework|redo|move|replace|split|merge)\b",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(
    r"^((?:@?[A-Za-z][\w.-]*(?:\s+[A-Za-z][\w.-]*)*,?\s*)+)",
)


def _parse_comment_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_comment_date(value: Any) -> date | None:
    dt = _parse_comment_dt(value)
    return dt.date() if dt else None


def _short_date(value: Any) -> str:
    d = _parse_comment_date(value)
    if d is None:
        return "?"
    return f"{d.day} {d.strftime('%b')}"


def _as_of_end(as_of: date) -> datetime:
    return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)


def _clip(text: str, limit: int = 110) -> str:
    msg = (text or "").strip().replace("\n", " ")
    if len(msg) > limit:
        return msg[: limit - 1].rstrip() + "…"
    return msg or "(empty)"


def load_figma_roster(path: Path | None = None) -> dict[str, dict[str, str]]:
    p = path or _ROSTER_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    handles = data.get("handles") or {}
    return {str(k): dict(v) for k, v in handles.items() if isinstance(v, dict)}


def _side_for_handle(handle: str, roster: dict[str, dict[str, str]]) -> str | None:
    h = (handle or "").strip()
    if not h:
        return None
    entry = roster.get(h)
    if entry:
        return entry.get("side")
    return None


def is_internal_handle(handle: str, roster: dict[str, dict[str, str]]) -> bool:
    side = _side_for_handle(handle, roster)
    return side == "internal"


def is_client_handle(handle: str, roster: dict[str, dict[str, str]]) -> bool:
    side = _side_for_handle(handle, roster)
    return side == "client"


def strip_leading_mentions(message: str, roster: dict[str, dict[str, str]]) -> str:
    """Strip @mention-like prefixes (plain text in Figma API)."""
    text = (message or "").strip()
    roster_names = sorted(roster.keys(), key=len, reverse=True)
    changed = True
    while changed and text:
        changed = False
        for name in roster_names:
            prefixes = [f"@{name}", name]
            for prefix in prefixes:
                if text.lower().startswith(prefix.lower()):
                    rest = text[len(prefix) :].lstrip(" ,:")
                    text = rest
                    changed = True
                    break
        m = _MENTION_RE.match(text)
        if m:
            candidate = m.group(1).strip().rstrip(",")
            if candidate in roster or "@" in candidate:
                text = text[m.end() :].lstrip(" ,:")
                changed = True
    return text.strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def is_question_shaped(message: str) -> bool:
    body = strip_leading_mentions(message, load_figma_roster())
    if _word_count(body) < 5:
        return False
    return bool(_QUESTION_RE.search(body))


def is_imperative_todo(message: str) -> bool:
    body = strip_leading_mentions(message, load_figma_roster())
    return bool(_IMPERATIVE_RE.match(body))


def _mentions_handle(message: str, handle: str) -> bool:
    h = handle.strip()
    if not h:
        return False
    low = (message or "").lower()
    return h.lower() in low or f"@{h.lower()}" in low


def _thread_messages(thread: dict[str, Any]) -> list[dict[str, Any]]:
    root = thread.get("root") or {}
    msgs = [root] if root else []
    for reply in thread.get("replies") or []:
        if isinstance(reply, dict):
            msgs.append(reply)
    return msgs


def thread_latest_comment(thread: dict[str, Any]) -> dict[str, Any]:
    msgs = _thread_messages(thread)
    if not msgs:
        return {}
    return max(
        msgs,
        key=lambda m: _parse_comment_dt(m.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


def thread_has_client(
    thread: dict[str, Any],
    roster: dict[str, dict[str, str]],
) -> bool:
    for msg in _thread_messages(thread):
        user = (msg.get("user") or "").strip()
        if is_client_handle(user, roster):
            return True
    return False


def classify_thread(
    thread: dict[str, Any],
    *,
    roster: dict[str, dict[str, str]],
) -> str | None:
    """Return UNANSWERED | NOT_CLOSED | NOT_FINISHED, or None to skip."""
    root = thread.get("root") or {}
    if root.get("resolved") or root.get("resolved_at"):
        return None
    msgs = _thread_messages(thread)
    if not msgs:
        return None
    last = thread_latest_comment(thread)
    last_user = (last.get("user") or "").strip()
    last_msg = str(last.get("message") or "")
    has_client = thread_has_client(thread, roster)

    if has_client:
        if is_client_handle(last_user, roster):
            return "UNANSWERED"
        for handle in roster:
            if is_client_handle(handle, roster) and _mentions_handle(last_msg, handle):
                return None
        if "?" in last_msg and any(
            is_internal_handle(h, roster) and _mentions_handle(last_msg, h)
            for h in roster
        ):
            return "UNANSWERED"
        return "NOT_CLOSED"
    # internal-only
    bodies = [strip_leading_mentions(last_msg, roster)]
    if len(msgs) > 1:
        bodies.append(strip_leading_mentions(str(root.get("message") or ""), roster))
    elif len(msgs) == 1:
        bodies[0] = strip_leading_mentions(str(root.get("message") or ""), roster)
    for body in bodies:
        if is_question_shaped(body) and (
            _addressed_to(last_msg, roster)
            or _addressed_to(str(root.get("message") or ""), roster)
            or "?" in body
            or re.search(r"\bcan you\b|\bcould you\b", body, re.I)
        ):
            return "UNANSWERED"
    if len(msgs) == 1:
        body = bodies[0]
        if is_imperative_todo(body):
            return "NOT_FINISHED"
    return None


def _node_link(file_url: str, comment: dict[str, Any]) -> str:
    meta = comment.get("client_meta") or {}
    node_id = meta.get("node_id")
    base = file_url.split("?")[0].rstrip("/")
    if node_id:
        return f"{base}?node-id={str(node_id).replace(':', '-')}"
    return base


def _thread_created_date(thread: dict[str, Any]) -> date | None:
    root = thread.get("root") or {}
    return _parse_comment_date(root.get("created_at"))


def _age_working_days(created: date | None, as_of: date) -> int:
    if created is None:
        return 0
    return working_days_between(created, as_of)


def _addressed_to(message: str, roster: dict[str, dict[str, str]]) -> str | None:
    body = str(message or "")
    for handle in roster:
        if _mentions_handle(body, handle):
            return handle
    return None


def _item_from_thread(
    thread: dict[str, Any],
    *,
    kind: str,
    file_key: str,
    file_url: str,
    as_of: date,
    roster: dict[str, dict[str, str]],
) -> dict[str, Any]:
    root = thread.get("root") or {}
    root_user = (root.get("user") or "?").strip()
    root_date = _thread_created_date(thread)
    root_msg = str(root.get("message") or "")
    quotes = [_clip(strip_leading_mentions(root_msg, roster))]
    for reply in thread.get("replies") or []:
        if isinstance(reply, dict):
            quotes.append(_clip(strip_leading_mentions(str(reply.get("message") or ""), roster)))
    quotes = [q for q in quotes if q and q != "(empty)"]
    link = _node_link(file_url, root)
    return {
        "kind": kind,
        "thread_id": str(root.get("id") or ""),
        "age_working_days": _age_working_days(root_date, as_of),
        "who": root_user,
        "to": _addressed_to(root_msg, roster),
        "date": root_date.isoformat() if root_date else "",
        "date_label": _short_date(root.get("created_at")),
        "quotes": quotes or [_clip(root_msg)],
        "quote": quotes[0] if quotes else _clip(root_msg),
        "link": link,
        "file_key": file_key,
        "file_url": file_url,
    }


def _collapse_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge same author + date into one entry with bulleted quotes."""
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for item in items:
        key = (item.get("who") or "", item.get("date") or "", item.get("kind") or "")
        if key not in buckets:
            buckets[key] = dict(item)
            buckets[key]["quotes"] = list(item.get("quotes") or [item.get("quote")])
            order.append(key)
        else:
            existing = buckets[key]
            for q in item.get("quotes") or [item.get("quote")]:
                if q and q not in existing["quotes"]:
                    existing["quotes"].append(q)
    out = []
    for key in order:
        row = buckets[key]
        row["quote"] = row["quotes"][0] if row["quotes"] else ""
        out.append(row)
    return out


def _count_week_activity(
    all_comments: list[dict[str, Any]],
    *,
    week_start: date,
    as_of: date,
    roster: dict[str, dict[str, str]],
) -> tuple[int, int, int]:
    new_comments = 0
    new_from_client = 0
    resolved_this_week = 0
    for c in all_comments:
        created = _parse_comment_date(c.get("created_at"))
        if created and week_start <= created <= as_of:
            new_comments += 1
            user = (c.get("user") or "").strip()
            if is_client_handle(user, roster):
                new_from_client += 1
        resolved_at = c.get("resolved_at")
        if resolved_at:
            rd = _parse_comment_date(resolved_at)
            if rd and week_start <= rd <= as_of and not c.get("parent_id"):
                resolved_this_week += 1
    return new_comments, new_from_client, resolved_this_week


def build_figma_project_output(
    threads: list[tuple[str, dict[str, Any]]],
    *,
    all_comments: list[dict[str, Any]],
    as_of: date,
    week_start: date,
    url_by_key: dict[str, str],
    roster: dict[str, dict[str, str]],
    unclassified: set[str],
) -> dict[str, Any]:
    overdue_raw: list[dict[str, Any]] = []
    this_week_raw: list[dict[str, Any]] = []

    for file_key, thread in threads:
        for msg in _thread_messages(thread):
            user = (msg.get("user") or "").strip()
            if user and user not in roster:
                unclassified.add(user)

        kind = classify_thread(thread, roster=roster)
        if not kind:
            continue
        file_url = url_by_key.get(file_key) or f"https://www.figma.com/design/{file_key}"
        item = _item_from_thread(
            thread,
            kind=kind,
            file_key=file_key,
            file_url=file_url,
            as_of=as_of,
            roster=roster,
        )
        created = _thread_created_date(thread)
        if created and created >= week_start:
            this_week_raw.append(item)
        else:
            overdue_raw.append(item)

    overdue = _collapse_items(
        sorted(overdue_raw, key=lambda x: (-x.get("age_working_days", 0), x.get("date") or ""))
    )
    this_week = _collapse_items(
        sorted(this_week_raw, key=lambda x: x.get("date") or "")
    )

    new_comments, new_from_client, resolved_this_week = _count_week_activity(
        all_comments, week_start=week_start, as_of=as_of, roster=roster
    )
    still_open = len(overdue_raw) + len(this_week_raw)
    overdue_items = [
        i for i in overdue_raw if i.get("age_working_days", 0) > _OVERDUE_WORKING_DAYS
    ]

    return {
        "counts": {
            "new_comments": new_comments,
            "new_from_client": new_from_client,
            "resolved_this_week": resolved_this_week,
            "still_open": still_open,
            "overdue_items": len(overdue_items),
        },
        "overdue": overdue,
        "this_week": this_week,
        "unclassified_handles": sorted(unclassified),
        "has_comments": bool(all_comments),
    }


def empty_figma_panel(*, has_urls: bool = True, configured: bool = True) -> dict[str, Any]:
    if not has_urls:
        return {
            "panel": "no_urls",
            "counts": {},
            "overdue": [],
            "this_week": [],
            "unclassified_handles": [],
        }
    if not configured:
        return {
            "panel": "not_configured",
            "counts": {},
            "overdue": [],
            "this_week": [],
            "unclassified_handles": [],
        }
    return {
        "panel": "empty",
        "counts": {
            "new_comments": 0,
            "new_from_client": 0,
            "resolved_this_week": 0,
            "still_open": 0,
            "overdue_items": 0,
        },
        "overdue": [],
        "this_week": [],
        "unclassified_handles": [],
        "has_comments": False,
    }


def figma_excerpt_no_urls() -> str:
    return "(none — no Figma files linked on Weekly health)"


def figma_excerpt_not_configured() -> str:
    return "(none — Figma auth not configured on Config)"


def figma_excerpt_no_activity(since: date) -> str:
    return (
        f"(none — no outstanding Figma items and no comments since "
        f"{since.isoformat()})"
    )


def format_figma_excerpt(panel: dict[str, Any], *, since: date) -> str:
    """LLM-facing summary from precomputed panel JSON."""
    if panel.get("panel") == "no_urls":
        return figma_excerpt_no_urls()
    if panel.get("panel") == "not_configured":
        return figma_excerpt_not_configured()
    counts = panel.get("counts") or {}
    if not counts.get("still_open") and not panel.get("has_comments"):
        if panel.get("panel") == "empty":
            return figma_excerpt_no_activity(since)
        return "No comments in the file."

    c = counts
    parts = [
        f"{c.get('new_comments', 0)} new",
    ]
    if c.get("new_from_client"):
        parts[0] += f" ({c['new_from_client']} from client)"
    parts.append(f"{c.get('resolved_this_week', 0)} resolved")
    parts.append(f"{c.get('still_open', 0)} still open")
    if c.get("overdue_items"):
        parts.append(f"{c['overdue_items']} overdue (>1 week)")
    lines = [" · ".join(parts)]
    for item in (panel.get("overdue") or [])[:5]:
        lines.append(
            f"- OVERDUE [{item.get('age_working_days')}d] {item.get('who')} "
            f"({item.get('date_label')}): {item.get('quote')} [{item.get('link')}]"
        )
    for item in (panel.get("this_week") or [])[:5]:
        lines.append(
            f"- THIS WEEK {item.get('who')} ({item.get('date_label')}): "
            f"{item.get('quote')} [{item.get('link')}]"
        )
    if panel.get("unclassified_handles"):
        lines.append(
            "WARNING unclassified handles: "
            + ", ".join(panel["unclassified_handles"])
        )
    return "\n".join(lines)


def empty_figma_bundle(excerpt: str) -> dict[str, Any]:
    return {
        "excerpt": excerpt,
        "panel": empty_figma_panel(has_urls="no Figma files" not in excerpt),
        "files": 0,
        "errors": [],
    }


def fetch_figma_comments_bundle(
    figma_urls: list[str],
    *,
    since: date,
    as_of: date | None = None,
    settings: Settings | None = None,
    account_domains: Iterable[str] | None = None,
    roster_emails: Iterable[str] | None = None,
    max_files: int = _DEFAULT_MAX_FILES,
    roster: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    from designops.adapters import figma as figma_api

    del account_domains, roster_emails  # roster file drives side classification
    if not figma_urls:
        return empty_figma_bundle(figma_excerpt_no_urls())

    s = settings or get_settings()
    if not figma_api.is_ready(s):
        return empty_figma_bundle(figma_excerpt_not_configured())

    snapshot = as_of or since
    week_start = snapshot - timedelta(days=7)
    roster_map = roster or load_figma_roster()
    unclassified: set[str] = set()
    selected: list[tuple[str, dict[str, Any]]] = []
    all_comments: list[dict[str, Any]] = []
    url_by_key: dict[str, str] = {}
    errors: list[str] = []
    files = 0

    for url in figma_urls[:max_files]:
        key = figma_api.extract_file_key(url)
        if key:
            url_by_key[key] = url.split("?")[0].rstrip("/")
        try:
            result = figma_api.fetch_file_comments(url, settings=s)
            files += 1
            file_key = str(
                result.get("file_key") or key or figma_api.extract_file_key(url) or url
            )
            if file_key and file_key not in url_by_key:
                url_by_key[file_key] = url.split("?")[0].rstrip("/")
            for thread in result.get("threads") or []:
                if isinstance(thread, dict):
                    selected.append((file_key, thread))
            for c in result.get("comments") or []:
                if isinstance(c, dict):
                    all_comments.append(figma_api.normalize_comment(c))
        except figma_api.FigmaError as e:
            errors.append(str(e))

    panel = build_figma_project_output(
        selected,
        all_comments=all_comments,
        as_of=snapshot,
        week_start=week_start,
        url_by_key=url_by_key,
        roster=roster_map,
        unclassified=unclassified,
    )
    panel["panel"] = "data"
    panel["files"] = files

    if errors and not all_comments:
        err = "; ".join(errors[:2])
        excerpt = f"(none — Figma fetch failed: {err})"
        panel = empty_figma_panel()
        panel["panel"] = "fetch_error"
        panel["fetch_error"] = err
        panel["files"] = files
    elif not all_comments:
        excerpt = "No comments in the file."
        panel = empty_figma_panel()
        panel["panel"] = "data"
        panel["has_comments"] = False
        panel["files"] = files
    else:
        excerpt = format_figma_excerpt(panel, since=since)

    return {
        "excerpt": excerpt,
        "panel": panel,
        "files": files,
        "errors": errors,
    }


def attach_figma_to_cards(
    cards: list[dict[str, Any]],
    figma_by_project: dict[str, dict[str, Any]],
) -> None:
    """Attach ``figma`` panel onto project cards for HTML."""
    for card in cards:
        bundle = figma_by_project.get(card.get("display_name") or "") or {}
        panel = dict(bundle.get("panel") or empty_figma_panel())
        if panel.get("panel") in {"no_urls", "not_configured"}:
            card.pop("figma", None)
            continue
        if bundle.get("errors") and not panel.get("has_comments"):
            panel["fetch_error"] = "; ".join(bundle["errors"][:2])
        card["figma"] = panel
        card.pop("figma_threads", None)


def sum_figma_overdue_kpi(figma_by_project: dict[str, dict[str, Any]]) -> int:
    total = 0
    for bundle in figma_by_project.values():
        counts = (bundle.get("panel") or {}).get("counts") or {}
        total += int(counts.get("overdue_items") or 0)
    return total


# --- Legacy helpers kept for adapter/tests that import them -----------------

def thread_latest_date(thread: dict[str, Any]) -> date | None:
    last = thread_latest_comment(thread)
    return _parse_comment_date(last.get("created_at"))


def thread_in_health_window(thread: dict[str, Any], since: date) -> bool:
    if thread.get("unresolved"):
        return True
    latest = thread_latest_date(thread)
    return latest is not None and latest >= since


def thread_sort_key(thread: dict[str, Any]) -> tuple:
    latest = thread_latest_date(thread) or date.min
    return (0 if thread.get("unresolved") else 1, -latest.toordinal())


def format_thread(thread: dict[str, Any], *, file_label: str) -> str:
    root = thread.get("root") or {}
    status = "OPEN" if thread.get("unresolved") else "resolved"
    user = (root.get("user") or "?").strip()
    msg = _clip(str(root.get("message") or ""), 200)
    return f"[{status}] {file_label} — {user} ({_short_date(root.get('created_at'))}): {msg}"


def is_internal_author(
    comment: dict[str, Any],
    *,
    roster_emails: Iterable[str] | None = None,
) -> bool:
    del roster_emails
    roster = load_figma_roster()
    user = (comment.get("user") or "").strip()
    return is_internal_handle(user, roster)


def is_client_author(
    comment: dict[str, Any],
    *,
    account_domains: Iterable[str] | None = None,
    roster_emails: Iterable[str] | None = None,
) -> bool:
    del account_domains, roster_emails
    roster = load_figma_roster()
    user = (comment.get("user") or "").strip()
    return is_client_handle(user, roster)


def is_plain_figma_risk(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12:
        return False
    if re.search(r"\b(unacked|>\s*24h|open_unresolved)\b", t, re.I):
        return False
    return True


def prefetch_figma_for_projects(
    projects: list[Any],
    *,
    comms_from: date,
    as_of: date | None = None,
    settings: Settings | None = None,
    coverage: dict[str, Any] | None = None,
    account_domains_by_project: dict[str, list[str]] | None = None,
    roster_emails: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    from designops.adapters import figma as figma_api

    s = settings or get_settings()
    cov = coverage if coverage is not None else {}
    results: dict[str, dict[str, Any]] = {}
    snapshot = as_of or comms_from

    if not figma_api.is_ready(s):
        cov["figma_note"] = "Figma not configured — Connect Figma or save PAT on Config"
        for proj in projects:
            urls = list(getattr(proj, "figma_urls", None) or [])
            results[proj.canonical_name] = empty_figma_bundle(
                figma_excerpt_not_configured() if urls else figma_excerpt_no_urls()
            )
        return results

    cov["figma_auth"] = figma_api.auth_mode(s)
    totals = {"projects_with_urls": 0, "files": 0, "overdue_items": 0, "errors": []}

    def _one(proj: Any) -> tuple[str, dict[str, Any]]:
        urls = list(getattr(proj, "figma_urls", None) or [])
        if not urls:
            return proj.canonical_name, empty_figma_bundle(figma_excerpt_no_urls())
        domains = (account_domains_by_project or {}).get(proj.canonical_name) or []
        return proj.canonical_name, fetch_figma_comments_bundle(
            urls,
            since=comms_from,
            as_of=snapshot,
            settings=s,
            account_domains=domains,
            roster_emails=roster_emails,
        )

    workers = max(1, min(int(s.fw_export_concurrency), len(projects) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, bundle in pool.map(_one, projects):
            results[name] = bundle
            if int(bundle.get("files") or 0) > 0:
                totals["projects_with_urls"] += 1
            totals["files"] += int(bundle.get("files") or 0)
            counts = (bundle.get("panel") or {}).get("counts") or {}
            totals["overdue_items"] += int(counts.get("overdue_items") or 0)
            for err in bundle.get("errors") or []:
                totals["errors"].append({"project": name, "error": err})

    cov["figma_prefetch"] = totals
    return results
