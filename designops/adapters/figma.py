"""Figma REST adapter — file comments (read) + stored PAT.

Auth preference: connected OAuth grant → personal access token saved on Config
(Postgres ``app_state``, not ``.env``).
OAuth uses Bearer; PAT uses `X-Figma-Token`.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from designops.adapters import figma_oauth
from designops.core.config import Settings, get_settings

# design | file | proto | board | slides | make | deck / {file_key}
_FILE_KEY_RE = re.compile(
    r"/(?:design|file|proto|board|slides|make|deck)/([a-zA-Z0-9]+)",
    re.I,
)

_PAT_STATE_KEY = "figma_pat"
_API_BASE = "https://api.figma.com"


class FigmaError(RuntimeError):
    pass


# --- Personal access token (Config UI → Postgres) ----------------------------


def _load_pat() -> dict | None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    try:
        with session_scope() as sess:
            row = sess.get(AppState, _PAT_STATE_KEY)
            if row and row.value:
                return dict(row.value)
    except Exception:  # noqa: BLE001
        pass
    return None


def get_pat() -> str | None:
    """Return the Config-stored Figma personal access token, if any."""
    d = _load_pat() or {}
    tok = (d.get("token") or "").strip()
    return tok or None


def pat_configured() -> bool:
    return bool(get_pat())


def pat_hint() -> str | None:
    """Masked hint for the Config UI (e.g. figd_…abcd)."""
    tok = get_pat()
    if not tok:
        return None
    if len(tok) <= 8:
        return "••••••••"
    return f"{tok[:5]}…{tok[-4:]}"


def save_pat(token: str) -> None:
    """Persist a personal access token from the Config page."""
    from designops.core.db import session_scope
    from designops.core.models import AppState

    tok = (token or "").strip()
    if not tok:
        raise FigmaError("token is empty")
    data = {"token": tok, "updated_at": time.time()}
    with session_scope() as sess:
        row = sess.get(AppState, _PAT_STATE_KEY)
        if row:
            row.value = data
        else:
            sess.add(AppState(key=_PAT_STATE_KEY, value=data))


def clear_pat() -> None:
    from designops.core.db import session_scope
    from designops.core.models import AppState

    with session_scope() as sess:
        row = sess.get(AppState, _PAT_STATE_KEY)
        if row:
            sess.delete(row)


def extract_file_key(url_or_key: str) -> str | None:
    """Return a Figma file key from a URL or bare key string."""
    raw = (url_or_key or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[a-zA-Z0-9]{10,}", raw) and "://" not in raw and "/" not in raw:
        return raw
    m = _FILE_KEY_RE.search(urlparse(raw).path or raw)
    return m.group(1) if m else None


def parse_figma_url_list(raw: str) -> list[str]:
    """Split a textarea (newlines / commas) into unique Figma URLs or bare keys.

    Drops blanks; keeps order. Raises FigmaError if a non-empty line isn't Figma.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[\n,]+", raw or ""):
        u = part.strip()
        if not u:
            continue
        if not extract_file_key(u):
            raise FigmaError(f"not a Figma file URL: {u[:120]}")
        # Prefer clean URL without query for storage when it's a full link.
        if "figma.com" in u.lower():
            u = u.split("?")[0].split("#")[0].rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def is_ready(settings: Settings | None = None) -> bool:
    """True if we can make authenticated Figma API calls right now."""
    s = settings or get_settings()
    if figma_oauth.is_connected(s):
        return True
    return pat_configured()


def auth_mode(settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    if figma_oauth.is_connected(s):
        return "oauth"
    if pat_configured():
        return "pat"
    return None


def _auth_headers(s: Settings) -> dict[str, str]:
    if figma_oauth.is_connected(s):
        return {"Authorization": f"Bearer {figma_oauth.access_token(s)}"}
    tok = get_pat()
    if tok:
        return {"X-Figma-Token": tok}
    raise FigmaError(
        "Figma not configured — Connect Figma on Config or save a personal access token"
    )


def get_file_meta(
    file_key: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """GET /v1/files/{key}/meta — name + last_touched_at (needs file_metadata:read)."""
    s = settings or get_settings()
    key = extract_file_key(file_key) or (file_key or "").strip()
    if not key:
        raise FigmaError("missing Figma file key")
    url = f"{_API_BASE}/v1/files/{key}/meta"
    try:
        r = httpx.get(url, headers=_auth_headers(s), timeout=20)
    except httpx.HTTPError as e:
        raise FigmaError(f"Figma meta request failed: {e}") from e
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("err") or r.json().get("message") or ""
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        raise FigmaError(f"Figma meta {r.status_code}: {detail or r.text[:160]}")
    body = r.json() or {}
    # Response may be wrapped as {file: {...}} or flat
    file_obj = body.get("file") if isinstance(body.get("file"), dict) else body
    return {
        "file_key": key,
        "name": file_obj.get("name"),
        "last_touched_at": file_obj.get("last_touched_at") or file_obj.get("lastModified"),
        "last_touched_by": _user_label(file_obj.get("last_touched_by")),
        "url": file_obj.get("url"),
    }


def enrich_urls_with_meta(
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Attach Figma last_touched_at / name when the token can read metadata."""
    s = settings or get_settings()
    if not is_ready(s):
        return rows
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        item = dict(row)
        if i >= limit:
            out.append(item)
            continue
        key = extract_file_key(str(item.get("url") or ""))
        if not key:
            out.append(item)
            continue
        try:
            meta = get_file_meta(key, settings=s)
            item["figma_name"] = meta.get("name")
            item["last_touched_at"] = meta.get("last_touched_at")
            item["last_touched_by"] = meta.get("last_touched_by")
        except FigmaError:
            # Scope missing or file inaccessible — leave fields unset
            pass
        out.append(item)
    return out


def _user_label(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("handle") or user.get("email") or user.get("id")


def _user_email(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    email = user.get("email")
    if isinstance(email, str) and "@" in email:
        return email.strip().lower()
    return None


def normalize_comment(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Figma Comment into a stable dict for pipelines / UI."""
    resolved_at = raw.get("resolved_at")
    user = raw.get("user")
    return {
        "id": str(raw.get("id") or ""),
        "message": str(raw.get("message") or ""),
        "user": _user_label(user),
        "user_email": _user_email(user),
        "created_at": raw.get("created_at"),
        "resolved_at": resolved_at,
        "resolved": bool(resolved_at),
        "parent_id": raw.get("parent_id") or None,
        "order_id": raw.get("order_id"),
        "client_meta": raw.get("client_meta"),
        "reactions": raw.get("reactions") or [],
    }


def thread_comments(
    comments: list[dict[str, Any]],
    *,
    unresolved_only: bool = False,
) -> list[dict[str, Any]]:
    """Group root comments with their replies (by parent_id).

    Each thread: {root, replies: [...], unresolved: bool}.
    Sorted by root created_at ascending (Figma pin order when present).
    """
    roots: dict[str, dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    for c in comments:
        n = normalize_comment(c) if "resolved" not in c else dict(c)
        cid = n.get("id") or ""
        parent = n.get("parent_id")
        if not parent:
            roots[cid] = {"root": n, "replies": [], "unresolved": not n.get("resolved")}
        else:
            orphans.append(n)

    for reply in orphans:
        parent = reply.get("parent_id")
        if parent in roots:
            roots[parent]["replies"].append(reply)
            # Thread stays "unresolved" if the root is unresolved.
        else:
            # Parent missing from payload — treat reply as its own root.
            roots[reply["id"]] = {
                "root": reply,
                "replies": [],
                "unresolved": not reply.get("resolved"),
            }

    threads = list(roots.values())
    for t in threads:
        t["replies"].sort(key=lambda r: r.get("created_at") or "")
    threads.sort(key=lambda t: t["root"].get("created_at") or "")

    if unresolved_only:
        threads = [t for t in threads if t.get("unresolved")]
    return threads


def get_comments(
    file_key: str,
    *,
    as_md: bool = True,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """GET /v1/files/{file_key}/comments — raw `comments` array from Figma."""
    s = settings or get_settings()
    key = extract_file_key(file_key) or (file_key or "").strip()
    if not key:
        raise FigmaError("missing Figma file key")
    params = {"as_md": "true"} if as_md else None
    url = f"{_API_BASE}/v1/files/{key}/comments"
    headers = _auth_headers(s)

    last_err: FigmaError | None = None
    for attempt in range(2):
        try:
            r = httpx.get(url, headers=headers, params=params, timeout=30)
        except httpx.HTTPError as e:
            raise FigmaError(f"Figma request failed: {e}") from e
        if r.status_code == 429:
            retry = r.headers.get("Retry-After", "2")
            try:
                wait = min(float(retry), 10.0)
            except ValueError:
                wait = 2.0
            last_err = FigmaError(f"Figma rate limited (429) — Retry-After: {retry}")
            if attempt == 0:
                time.sleep(wait)
                continue
            raise last_err
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("err") or r.json().get("message") or ""
            except Exception:  # noqa: BLE001
                detail = r.text[:300]
            raise FigmaError(f"Figma {r.status_code}: {detail or r.text[:200]}")
        body = r.json()
        comments = body.get("comments")
        return list(comments) if isinstance(comments, list) else []
    raise last_err or FigmaError("Figma request failed")


def recent_comments(
    raw: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize and sort comments newest-first; optionally cap with ``limit``."""
    flat = [normalize_comment(c) for c in raw]
    flat.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    if limit is not None and limit > 0:
        return flat[: int(limit)]
    return flat


def fetch_file_comments(
    url_or_key: str,
    *,
    unresolved_only: bool = False,
    as_md: bool = True,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fetch + normalize comments for a file URL or key.

    Returns:
      {
        file_key, auth_mode, total, unresolved_roots, thread_count,
        threads: [...], comments: [...] (newest-first; capped when limit set)
      }
    """
    s = settings or get_settings()
    key = extract_file_key(url_or_key)
    if not key:
        raise FigmaError("could not parse Figma file key from URL")
    raw = get_comments(key, as_md=as_md, settings=s)
    all_threads = thread_comments(raw)
    unresolved_roots = sum(1 for t in all_threads if t.get("unresolved"))
    threads = (
        [t for t in all_threads if t.get("unresolved")]
        if unresolved_only
        else all_threads
    )
    comments = recent_comments(raw, limit=limit)
    out: dict[str, Any] = {
        "file_key": key,
        "auth_mode": auth_mode(s),
        "total": len(raw),
        "unresolved_roots": unresolved_roots,
        "thread_count": len(threads),
        "threads": threads,
        "comments": comments,
    }
    if limit is not None and limit > 0:
        out["limit"] = int(limit)
    return out
