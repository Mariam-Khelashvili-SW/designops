"""Tempo Cloud worklogs adapter (VACSICK leave detection).

Auth is a Tempo Bearer token from env — never the DB, never git.
Tempo worklog payloads expose issue ``id`` only (not key/project); callers
resolve IDs via Jira when filtering to VACSICK.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx

from designops.core.config import Settings, get_settings

VACSICK_PROJECT_KEY = "VACSICK"


def _parse_worklog_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _account_id_from_worklog(row: dict) -> str | None:
    author = row.get("author") or {}
    if isinstance(author, dict):
        aid = author.get("accountId")
        if aid:
            return str(aid)
    for key in ("authorAccountId", "workerId", "worker"):
        v = row.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("accountId"):
            return str(v["accountId"])
    return None


def _issue_key_from_worklog(row: dict) -> str | None:
    issue = row.get("issue") or {}
    if isinstance(issue, dict):
        key = issue.get("key")
        if key:
            return str(key).upper()
    key = row.get("issueKey") or row.get("issue_key")
    return str(key).upper() if key else None


def _issue_id_from_worklog(row: dict) -> str | None:
    issue = row.get("issue") or {}
    if isinstance(issue, dict) and issue.get("id") is not None:
        return str(issue["id"])
    if row.get("issueId") is not None:
        return str(row["issueId"])
    return None


def _is_vacsick_worklog(row: dict, *, project_key: str = VACSICK_PROJECT_KEY) -> bool:
    """True when Tempo payload itself carries project/key (often false on Cloud)."""
    pk = project_key.upper()
    issue = row.get("issue") or {}
    if isinstance(issue, dict):
        proj = issue.get("projectKey") or issue.get("project_key")
        if proj and str(proj).upper() == pk:
            return True
        nested = issue.get("project") or {}
        if isinstance(nested, dict) and (nested.get("key") or "").upper() == pk:
            return True
    key = _issue_key_from_worklog(row) or ""
    return key.startswith(f"{pk}-")


def normalize_tempo_worklog(row: dict) -> dict | None:
    """Normalize a Tempo worklog payload into a flat dict, or None if unusable."""
    if not isinstance(row, dict):
        return None
    account_id = _account_id_from_worklog(row)
    started = _parse_worklog_date(
        row.get("startDate")
        or row.get("started")
        or row.get("startDateTimeOffset")
        or row.get("dateStarted")
    )
    secs = row.get("timeSpentSeconds")
    if secs is None:
        secs = row.get("billableSeconds")
    try:
        seconds = int(secs) if secs is not None else 0
    except (TypeError, ValueError):
        seconds = 0
    if not account_id or started is None or seconds <= 0:
        return None
    desc = row.get("description")
    if desc is not None and not isinstance(desc, str):
        desc = str(desc)
    return {
        "account_id": account_id,
        "started": started,
        "time_spent_seconds": seconds,
        "hours": round(seconds / 3600.0, 2),
        "issue_key": _issue_key_from_worklog(row),
        "issue_id": _issue_id_from_worklog(row),
        "description": (desc or "").strip() or None,
        "raw": row,
    }


class TempoClient:
    """Read-only Tempo Cloud worklogs client."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _client(self) -> httpx.Client:
        base = (self.settings.tempo_api_base or "https://api.tempo.io/4").rstrip("/")
        return httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {self.settings.tempo_api_token}",
                "Accept": "application/json",
            },
            timeout=60.0,
        )

    def list_worklogs(
        self,
        *,
        from_date: date,
        to_date: date,
        project: str | None = None,
        account_ids: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Fetch Tempo worklogs in [from_date, to_date].

        Prefer ``account_ids`` (``/worklogs/user/{id}``). Tempo Cloud does not
        reliably accept ``project=VACSICK`` and omits issue keys — returns all
        user worklogs normalized; callers filter to VACSICK via Jira issue ids.
        """
        if not self.settings.tempo_configured:
            return []

        ids = [a for a in (account_ids or []) if a]
        if ids:
            rows: list[dict] = []
            for aid in ids:
                rows.extend(
                    self._paginate(
                        f"/worklogs/user/{aid}",
                        {
                            "from": from_date.isoformat(),
                            "to": to_date.isoformat(),
                            "limit": min(limit, 1000),
                        },
                    )
                )
        else:
            params: dict[str, Any] = {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "limit": min(limit, 1000),
            }
            # project=KEY returns 400 on this Tempo Cloud tenant — skip it
            rows = self._paginate("/worklogs", params)

        out: list[dict] = []
        for row in rows:
            # project= filter is applied by callers after Jira resolves issue ids
            # (Tempo Cloud payloads usually lack key/projectKey).
            _ = project
            norm = normalize_tempo_worklog(row)
            if norm:
                out.append(norm)
        return out

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict]:
        results: list[dict] = []
        offset = 0
        limit = int(params.get("limit") or 1000)
        with self._client() as c:
            while True:
                q = dict(params)
                q["offset"] = offset
                q["limit"] = limit
                r = c.get(path, params=q)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    results.extend(data)
                    break
                batch = data.get("results") or data.get("values") or data.get("worklogs") or []
                results.extend(batch)
                meta = data.get("metadata") or {}
                count = meta.get("count")
                if not batch:
                    break
                offset += len(batch)
                next_url = meta.get("next") or data.get("next")
                if isinstance(next_url, str) and next_url.startswith("http"):
                    # Follow Tempo absolute next links until exhausted
                    while isinstance(next_url, str) and next_url.startswith("http"):
                        r2 = c.get(next_url)
                        r2.raise_for_status()
                        data2 = r2.json()
                        batch2 = (
                            data2.get("results")
                            or data2.get("values")
                            or data2.get("worklogs")
                            or []
                        )
                        results.extend(batch2)
                        next_url = (data2.get("metadata") or {}).get("next")
                        if not batch2:
                            break
                    break
                if count is not None and offset >= int(count):
                    break
                if len(batch) < limit:
                    break
                if offset > 20000:
                    break
        return results
