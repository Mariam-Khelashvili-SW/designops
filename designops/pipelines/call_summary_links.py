"""Resolve call-summary artifact placeholders via Fairwind accounts + Jira.

Scope is the **single Jira project for this call** (from the meeting title /
Fairwind board name), never every board on the client account. Searching
SGDB2B + SGDCP + SGD together would mix product lines.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from designops.core.config import get_settings
from designops.core.models import Account

log = logging.getLogger("designops.call_summary.links")

FIGMA_URL_RE = re.compile(r"https?://(?:www\.)?figma\.com/[^\s\"'<>\]]+", re.I)
NOTION_URL_RE = re.compile(r"https?://(?:www\.)?notion\.(?:so|site)/[^\s\"'<>\]]+", re.I)
HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\]]+", re.I)

# Meeting-title hints → one Jira key (checked in order; first match wins).
_HINT_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bclub\s*portal\b|\bsgdcp\b", re.I), "SGDCP"),
    (re.compile(r"\bb2b\b|\bsgdb2b\b", re.I), "SGDB2B"),
    (re.compile(r"\bphase\s*1\b|\bsgdp1\b", re.I), "SGDP1"),
    (re.compile(r"\bmultibrand\b|\bservice\s*cloud\b", re.I), "SGD"),
]


def find_account_for_project(session: Session, project_name: str) -> Account | None:
    """Match Fairwind-synced Account by name / alias substring."""
    pname = (project_name or "").strip()
    if not pname:
        return None
    accounts = session.query(Account).all()
    pl = pname.lower()
    scored: list[tuple[int, Account]] = []
    for acct in accounts:
        names = [acct.name or "", *(acct.aliases or [])]
        for n in names:
            nl = n.lower().strip()
            if not nl:
                continue
            if nl == pl:
                scored.append((1000, acct))
            elif nl in pl or pl in nl:
                scored.append((len(nl), acct))
            else:
                nt = set(re.findall(r"[a-z0-9]+", nl))
                pt = set(re.findall(r"[a-z0-9]+", pl))
                if len(nt) >= 2 and nt.issubset(pt):
                    scored.append((len(nl), acct))
                elif len(pt) >= 2 and pt.issubset(nt):
                    scored.append((len(pt), acct))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def resolve_call_jira_key(
    account_keys: list[str],
    *,
    meeting_title: str = "",
    fairwind_jira_projects: list[dict] | None = None,
) -> str | None:
    """Pick the **one** Jira project for this call.

    Uses the meeting title (and Fairwind board names) only — not artifact text —
    so a B2B weekly does not pull Club Portal / Phase 1 boards. Returns None when
    ambiguous (caller must leave placeholders rather than search every key).
    """
    keys = [str(k).strip().upper() for k in account_keys if str(k).strip()]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]

    title = meeting_title or ""
    for pat, key in _HINT_KEY_PATTERNS:
        if pat.search(title) and key in keys:
            return key

    # Match Fairwind board display names against the meeting title (longest win).
    best_key: str | None = None
    best_score = 0
    for jp in fairwind_jira_projects or []:
        if not isinstance(jp, dict):
            continue
        key = str(jp.get("key") or "").strip().upper()
        name = str(jp.get("name") or "").strip()
        if not key or key not in keys or not name:
            continue
        # Distinctive tokens from board name (skip generic "Sports Group Denmark")
        tokens = [
            t
            for t in re.findall(r"[a-z0-9]+", name.lower())
            if t not in {"sports", "group", "denmark", "phase", "the", "and", "for"}
            and len(t) >= 2
        ]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", title, re.I))
        if hits > 0 and (hits > best_score or (hits == best_score and len(name) > best_score)):
            # Prefer more token hits; tie-break longer name
            score = hits * 100 + len(name)
            if score > best_score:
                best_score = score
                best_key = key
    if best_key:
        return best_key

    return None


def prefer_jira_keys(
    account_keys: list[str],
    *,
    meeting_title: str = "",
    artifact_names: list[str] = (),  # kept for call-site compat; ignored for scope
    fairwind_jira_projects: list[dict] | None = None,
) -> list[str]:
    """Return at most one Jira key — the project invited on this call."""
    del artifact_names  # intentionally unused: do not widen scope via artifacts
    key = resolve_call_jira_key(
        account_keys,
        meeting_title=meeting_title,
        fairwind_jira_projects=fairwind_jira_projects,
    )
    return [key] if key else []


def _walk_adf(node: Any, texts: list[str], urls: list[str]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            texts.append(str(node["text"]))
        for mark in node.get("marks") or []:
            if isinstance(mark, dict) and mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href")
                if href:
                    urls.append(str(href))
        if node.get("type") in ("inlineCard", "blockCard", "embedCard"):
            href = (node.get("attrs") or {}).get("url")
            if href:
                urls.append(str(href))
        for v in node.values():
            _walk_adf(v, texts, urls)
    elif isinstance(node, list):
        for child in node:
            _walk_adf(child, texts, urls)


def extract_urls_from_adf(description: Any) -> tuple[str, list[str]]:
    """Return (plain text, urls) from a Jira ADF description."""
    texts: list[str] = []
    urls: list[str] = []
    _walk_adf(description, texts, urls)
    blob = "\n".join(texts)
    # Also pick up bare URLs typed into text nodes
    for m in HTTP_URL_RE.findall(blob):
        urls.append(m.rstrip(").,;]}"))
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen and u.startswith("http"):
            seen.add(u)
            out.append(u)
    return blob, out


def _jql_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def _artifact_kind(name: str, platform: str = "") -> str:
    blob = f"{name} {platform}".lower()
    if "figma" in blob or "wireframe" in blob or "prototype" in blob or "design file" in blob:
        return "figma"
    if "notion" in blob or "brief" in blob:
        return "notion"
    if "road" in blob and "map" in blob:
        return "roadmap"
    if "staging" in blob or "preview" in blob:
        return "staging"
    return "other"


def _token_overlap(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    tb = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _browse_url(base: str, key: str) -> str:
    return f"{base.rstrip('/')}/browse/{key}"


def resolve_links_from_jira(
    *,
    jira_keys: list[str],
    artifacts: list[dict],
    meeting_title: str = "",
    fairwind_jira_projects: list[dict] | None = None,
    jira_client: Any | None = None,
) -> dict[str, str]:
    """Return artifact-name → URL for unresolved artifacts, when Jira finds a match.

    Searches only the call's Jira project (one key). Never invents URLs.
    """
    settings = get_settings()
    if not settings.jira_configured or not jira_keys:
        return {}

    try:
        from designops.adapters.jira import JiraClient

        client = jira_client or JiraClient(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("Jira client unavailable for link resolve: %s", e)
        return {}

    base = (settings.jira_base_url or "").rstrip("/")
    keys = prefer_jira_keys(
        jira_keys,
        meeting_title=meeting_title,
        fairwind_jira_projects=fairwind_jira_projects,
    )
    if not keys:
        log.info(
            "No single Jira project matched meeting %r — skipping link harvest",
            (meeting_title or "")[:80],
        )
        return {}

    # Exactly one project for this call
    call_key = keys[0]
    key_clause = call_key
    notes: list[str] = [f"Link lookup scoped to Jira project {call_key} (from call title)."]

    resolved: dict[str, str] = {}
    project_notion: str | None = None
    project_figma: str | None = None

    # Harvest common project assets from design tickets once
    try:
        harvest_jql = (
            f"project = {key_clause} AND "
            f'(summary ~ "UI design" OR summary ~ "Design and UI" OR text ~ "figma.com" '
            f'OR text ~ "notion.so") '
            f"ORDER BY updated DESC"
        )
        harvested = client.search_jql(harvest_jql, max_results=20, limit=20)
        for issue in harvested:
            fields = issue.get("fields") or {}
            _text, urls = extract_urls_from_adf(fields.get("description"))
            for u in urls:
                if not project_figma and FIGMA_URL_RE.match(u):
                    project_figma = u.split("?")[0]
                if not project_notion and NOTION_URL_RE.match(u):
                    project_notion = u.split("?")[0]
            if project_figma and project_notion:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("Jira harvest search failed: %s", e)

    for art in artifacts:
        name = str(art.get("name") or "").strip()
        if not name:
            continue
        kind = _artifact_kind(name, str(art.get("platform") or ""))
        terms = [
            t
            for t in re.findall(r"[A-Za-z]{3,}", name)
            if t.lower() not in {"the", "and", "for", "from"}
        ]
        terms = terms[:4] or [name[:40]]
        term_jql = " OR ".join(f'summary ~ "{_jql_escape(t)}"' for t in terms)
        if kind == "roadmap":
            term_jql = f'(summary ~ "roadmap" OR summary ~ "road map" OR ({term_jql}))'
        elif kind == "figma":
            term_jql = (
                f'(summary ~ "wireframe" OR summary ~ "figma" OR summary ~ "UI design" '
                f"OR ({term_jql}))"
            )

        try:
            jql = f"project = {key_clause} AND ({term_jql}) ORDER BY updated DESC"
            issues = client.search_jql(jql, max_results=10, limit=10)
        except Exception as e:  # noqa: BLE001
            log.warning("Jira artifact search failed for %r: %s", name, e)
            issues = []

        best_url: str | None = None
        best_score = 0.0
        for issue in issues:
            key = issue.get("key") or ""
            fields = issue.get("fields") or {}
            summary = str(fields.get("summary") or "")
            _text, urls = extract_urls_from_adf(fields.get("description"))
            figma_urls = [u for u in urls if FIGMA_URL_RE.match(u)]
            notion_urls = [u for u in urls if NOTION_URL_RE.match(u)]
            score = _token_overlap(name, summary)
            if kind == "roadmap" and re.search(r"road\s*map", summary, re.I):
                score += 0.6
            if kind == "figma" and re.search(r"wireframe|figma|ui design", summary, re.I):
                score += 0.4

            candidate = None
            if kind == "figma":
                candidate = figma_urls[0] if figma_urls else None
            elif kind == "notion":
                candidate = notion_urls[0] if notion_urls else None
            elif kind == "roadmap":
                candidate = notion_urls[0] if notion_urls else (figma_urls[0] if figma_urls else None)
                if not candidate and key and score >= 0.5:
                    candidate = _browse_url(base, key)
            else:
                candidate = figma_urls[0] if figma_urls else (notion_urls[0] if notion_urls else None)

            if candidate and score >= best_score:
                best_score = score
                best_url = candidate

        if not best_url or best_score < 0.35:
            if kind == "figma" and project_figma:
                best_url = project_figma
                best_score = max(best_score, 0.4)
                notes.append(f"Resolved “{name}” via {call_key} Figma file from Jira.")
            elif kind in ("roadmap", "notion", "other") and project_notion:
                if kind in ("roadmap", "notion") or "project" in name.lower():
                    best_url = project_notion
                    best_score = max(best_score, 0.4)
                    notes.append(f"Resolved “{name}” via {call_key} Notion page from Jira.")

        if best_url and best_score >= 0.35:
            resolved[name] = best_url
            if kind == "roadmap" and best_url.startswith(base):
                notes.append(
                    f"Resolved “{name}” to Jira {best_url.rsplit('/', 1)[-1]} — "
                    "confirm this is the board/doc shown on the call."
                )

    if project_notion:
        resolved.setdefault("notion_brief", project_notion)
    if project_figma:
        resolved.setdefault("_project_figma", project_figma)

    if notes:
        resolved["_reviewer_notes"] = "\n".join(notes)  # type: ignore[assignment]
    return resolved


def apply_jira_fairwind_to_link_map(
    link_map: dict[str, str],
    *,
    session: Session,
    project_name: str,
    artifacts: list[dict],
    meeting_title: str = "",
    jira_client: Any | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Fill remaining placeholders using the call's single Jira project.

    Fairwind account → jira keys, then pick **one** key from the meeting title.
    Returns (updated link_map, reviewer_notes). Failures are non-fatal.
    """
    try:
        return _apply_jira_fairwind_to_link_map(
            link_map,
            session=session,
            project_name=project_name,
            artifacts=artifacts,
            meeting_title=meeting_title,
            jira_client=jira_client,
        )
    except Exception as e:  # noqa: BLE001 — never fail draft generation on link lookup
        log.warning("Jira/Fairwind link resolve skipped: %s", e)
        return dict(link_map), [f"Link lookup skipped: {e}"]


def _apply_jira_fairwind_to_link_map(
    link_map: dict[str, str],
    *,
    session: Session,
    project_name: str,
    artifacts: list[dict],
    meeting_title: str = "",
    jira_client: Any | None = None,
) -> tuple[dict[str, str], list[str]]:
    out = dict(link_map)
    notes: list[str] = []
    unresolved = [
        a
        for a in artifacts
        if not str(out.get(str(a.get("name") or ""), "")).startswith("http")
    ]

    acct = find_account_for_project(session, project_name)
    if acct is None:
        return out, notes

    keys = [str(k).upper() for k in (acct.jira_project_keys or []) if str(k).strip()]
    fw_projects: list[dict] = []
    try:
        settings = get_settings()
        if settings.fairwind_configured:
            from designops.adapters.fairwind import FairwindClient

            fw = FairwindClient(settings)
            for row in fw.list_accounts():
                if row.get("id") != acct.fairwind_account_id:
                    continue
                for jp in row.get("jira_projects") or []:
                    if isinstance(jp, dict) and jp.get("key"):
                        fw_projects.append(jp)
                        k = str(jp["key"]).upper()
                        if k not in keys:
                            keys.append(k)
                    elif isinstance(jp, str):
                        k = jp.upper()
                        if k not in keys:
                            keys.append(k)
                        fw_projects.append({"key": k, "name": k})
                break
    except Exception as e:  # noqa: BLE001
        log.warning("Fairwind jira_projects enrich failed: %s", e)

    if not keys:
        return out, notes

    call_key = resolve_call_jira_key(
        keys,
        meeting_title=meeting_title,
        fairwind_jira_projects=fw_projects or None,
    )
    if not call_key:
        notes.append(
            "Could not determine which Jira project this call belongs to "
            f"from the title — left link placeholders. Account boards: {', '.join(keys)}."
        )
        return out, notes

    found = resolve_links_from_jira(
        jira_keys=[call_key],
        artifacts=unresolved or artifacts,
        meeting_title=meeting_title,
        fairwind_jira_projects=fw_projects or None,
        jira_client=jira_client,
    )
    extra_notes = found.pop("_reviewer_notes", None)
    if isinstance(extra_notes, str) and extra_notes.strip():
        notes.extend(extra_notes.split("\n"))

    for name, url in found.items():
        if name.startswith("_"):
            continue
        if name == "notion_brief":
            out.setdefault("notion_brief", url)
            continue
        current = out.get(name, "")
        if current.startswith("http"):
            continue
        if url.startswith("http"):
            out[name] = url

    project_figma = found.get("_project_figma")
    if project_figma:
        for art in artifacts:
            name = str(art.get("name") or "")
            if not name:
                continue
            if _artifact_kind(name, str(art.get("platform") or "")) == "figma":
                if not str(out.get(name, "")).startswith("http"):
                    out[name] = project_figma

    out["_call_jira_key"] = call_key
    return out, notes


def inject_resolved_urls_into_body(
    body: str,
    link_map: dict[str, str],
    artifacts: list[dict],
) -> str:
    """Replace ``[link]`` / ``[Figma link]`` when the line matches a resolved artifact."""
    if not body:
        return body

    resolved: list[tuple[str, str]] = []
    for art in artifacts:
        name = str(art.get("name") or "").strip()
        url = link_map.get(name, "")
        if name and url.startswith("http"):
            resolved.append((name, url))
    # Also consider notion_brief for roadmap-ish lines
    notion = link_map.get("notion_brief", "")
    if notion.startswith("http"):
        resolved.append(("project road map", notion))
        resolved.append(("roadmap", notion))

    if not resolved:
        return body

    lines = body.split("\n")
    out_lines: list[str] = []
    placeholder_re = re.compile(r"\[(Figma link|link)\]", re.I)

    for line in lines:
        if not placeholder_re.search(line):
            out_lines.append(line)
            continue
        line_l = line.lower()
        best_url = None
        best_score = 0.0
        for name, url in resolved:
            score = _token_overlap(name, line_l)
            # Strong boost when distinctive tokens from the name appear
            if score > best_score:
                best_score = score
                best_url = url
        if best_url and best_score >= 0.35:
            out_lines.append(placeholder_re.sub(best_url, line, count=1))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def scrub_tracking_params(url: str) -> str:
    """Drop noisy Notion/Figma tracking query params for cleaner drafts."""
    if "notion.so" in url or "notion.site" in url:
        return url.split("?")[0]
    if "figma.com" in url and "node-id=" not in url:
        return url.split("?")[0]
    return url


def _clean_figma_url(url: str) -> str | None:
    u = (url or "").strip()
    if not u or not FIGMA_URL_RE.match(u):
        return None
    return u.split("?")[0].split("#")[0].rstrip("/")


def harvest_figma_urls_from_jira(
    jira_keys: list[str],
    *,
    client: Any | None = None,
    max_issues_per_key: int = 40,
) -> dict[str, Any]:
    """Scan Jira issues for figma.com links under the given project keys.

    Returns ``{urls: [{url, issue_key, summary, source}], keys_searched, errors}``.
    Fairwind is used upstream only to discover keys; links themselves live in Jira.
    """
    from designops.adapters.jira import JiraClient

    keys = []
    seen_k: set[str] = set()
    for k in jira_keys or []:
        kk = (k or "").strip().upper()
        if kk and kk not in seen_k:
            seen_k.add(kk)
            keys.append(kk)

    out: list[dict[str, str]] = []
    seen_url: set[str] = set()
    errors: list[str] = []
    if not keys:
        return {"urls": out, "keys_searched": keys, "errors": ["no Jira project key"]}

    jc = client or JiraClient(get_settings())
    for key in keys:
        jql = (
            f"project = {key} AND "
            f'(summary ~ "figma" OR summary ~ "wireframe" OR summary ~ "UI design" '
            f'OR summary ~ "Design and UI" OR text ~ "figma.com") '
            f"ORDER BY updated DESC"
        )
        try:
            issues = jc.search_jql(
                jql,
                max_results=min(50, max_issues_per_key),
                limit=max_issues_per_key,
                fields=["summary", "description"],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Figma harvest failed for %s: %s", key, e)
            errors.append(f"{key}: {e}")
            continue
        for issue in issues:
            ikey = str(issue.get("key") or "")
            fields = issue.get("fields") or {}
            summary = str(fields.get("summary") or "")
            _text, urls = extract_urls_from_adf(fields.get("description"))
            # Bare figma URLs sometimes appear only in the summary
            for m in FIGMA_URL_RE.findall(summary):
                urls.append(m)
            for raw_u in urls:
                cleaned = _clean_figma_url(raw_u)
                if not cleaned or cleaned in seen_url:
                    continue
                seen_url.add(cleaned)
                out.append(
                    {
                        "url": cleaned,
                        "issue_key": ikey,
                        "summary": summary[:120],
                        "jira_project": key,
                        "source": "jira",
                    }
                )
    return {"urls": out, "keys_searched": keys, "errors": errors}


def harvest_figma_urls_from_fairwind(
    fairwind_account_id: str,
    *,
    jira_keys: list[str] | None = None,
    corpus_dir: str | None = None,
) -> dict[str, Any]:
    """Scan cached Fairwind export corpus (Jira JSON) for figma.com links.

    Uses already-downloaded ``var/corpus/{account_id}/**/json/jira`` files — no new
    Fairwind export. Optional ``jira_keys`` filters to those project keys.
    """
    from pathlib import Path

    import json

    from designops.core.config import get_settings as _gs

    aid = (fairwind_account_id or "").strip()
    out: list[dict[str, str]] = []
    errors: list[str] = []
    if not aid:
        return {"urls": out, "keys_searched": [], "errors": ["no Fairwind account id"]}

    key_filter = {k.strip().upper() for k in (jira_keys or []) if (k or "").strip()}
    root = Path(corpus_dir or _gs().corpus_store_dir) / aid
    if not root.is_dir():
        return {
            "urls": out,
            "keys_searched": sorted(key_filter),
            "errors": [f"no Fairwind corpus for account {aid}"],
        }

    seen_url: set[str] = set()
    files_scanned = 0
    for path in root.rglob("*.json"):
        # Prefer issue files under json/jira; skip projects.json aggregates
        parts = {p.lower() for p in path.parts}
        if "jira" not in parts:
            continue
        if path.name == "projects.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records: list[dict]
        if isinstance(data, list):
            records = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            if data.get("key") or data.get("summary") or data.get("description_text"):
                records = [data]
            else:
                records = [
                    r
                    for r in (data.get("issues") or data.get("items") or [])
                    if isinstance(r, dict)
                ]
        else:
            continue

        for issue in records:
            files_scanned += 1
            pk = str(issue.get("project_key") or "").upper()
            ikey = str(issue.get("key") or "")
            if key_filter:
                # Prefer project_key; fall back to issue key prefix (PAARS-123 → PAARS)
                prefix = ikey.split("-", 1)[0].upper() if ikey else ""
                if pk and pk not in key_filter and prefix not in key_filter:
                    continue
            summary = str(issue.get("summary") or issue.get("title") or "")
            blob = "\n".join(
                str(issue.get(f) or "")
                for f in ("description_text", "description", "body", "summary", "title")
            )
            urls = list(FIGMA_URL_RE.findall(blob))
            for raw_u in urls:
                cleaned = _clean_figma_url(raw_u)
                if not cleaned or cleaned in seen_url:
                    continue
                seen_url.add(cleaned)
                out.append(
                    {
                        "url": cleaned,
                        "issue_key": ikey,
                        "summary": summary[:120],
                        "jira_project": pk or (ikey.split("-", 1)[0] if ikey else ""),
                        "source": "fairwind",
                    }
                )

    if files_scanned == 0 and not out:
        errors.append(f"no Jira issues in Fairwind corpus for {aid}")
    return {
        "urls": out,
        "keys_searched": sorted(key_filter),
        "errors": errors,
        "files_scanned": files_scanned,
    }


def merge_figma_url_hits(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by URL; combine sources when the same file appears in Jira + Fairwind."""
    by_url: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for row in batch or []:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            if url not in by_url:
                by_url[url] = dict(row)
                src = row.get("source") or "unknown"
                by_url[url]["sources"] = [src] if isinstance(src, str) else list(src or [])
                continue
            cur = by_url[url]
            src = row.get("source")
            sources = list(cur.get("sources") or [])
            if src and src not in sources:
                sources.append(src)
            cur["sources"] = sources
            # Prefer a non-empty issue key / summary from either side
            if not cur.get("issue_key") and row.get("issue_key"):
                cur["issue_key"] = row["issue_key"]
            if not cur.get("summary") and row.get("summary"):
                cur["summary"] = row["summary"]
            if not cur.get("jira_project") and row.get("jira_project"):
                cur["jira_project"] = row["jira_project"]
    return list(by_url.values())
