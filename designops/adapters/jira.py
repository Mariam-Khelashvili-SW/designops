"""Direct Atlassian Jira Cloud adapter (A3 weekly backlog + A2 project health).

Read-only: search issues by assignee / key / project + estimate/logged hours.
Auth is email + API token (Basic) from env — never the DB, never git.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx

from designops.adapters.documents import Document
from designops.core.config import Settings, get_settings
from designops.core.enums import TIME_LOG_BUCKET_TYPES

# Fields for A3 load math + A2 health burn / ageing.
_ISSUE_FIELDS = [
    "summary",
    "status",
    "assignee",
    "timetracking",
    "timeestimate",
    "timeoriginalestimate",
    "timespent",
    "aggregatetimespent",
    "duedate",
    "created",
    "updated",
    "project",
    "issuetype",
    "description",
    "components",
    "parent",
    "comment",
]

# Health pulls need changelog for status-entry dates (client-action ageing).
_HEALTH_EXPAND = ["changelog"]


def _seconds_from(fields: dict, *, tt_keys: tuple[str, ...], field_key: str) -> int | None:
    """Prefer timetracking.*Seconds, fall back to a top-level seconds field."""
    if not fields:
        return None
    tt = fields.get("timetracking") or {}
    if isinstance(tt, dict):
        for key in tt_keys:
            v = tt.get(key)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
    v = fields.get(field_key)
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def remaining_seconds_from_issue(fields: dict) -> int | None:
    """Extract remaining estimate in seconds from a Jira issue fields dict."""
    return _seconds_from(
        fields,
        tt_keys=("remainingEstimateSeconds", "remainingEstimateInSeconds"),
        field_key="timeestimate",
    )


def original_seconds_from_issue(fields: dict) -> int | None:
    """Original / planned estimate in seconds."""
    return _seconds_from(
        fields,
        tt_keys=("originalEstimateSeconds", "originalEstimateInSeconds"),
        field_key="timeoriginalestimate",
    )


def spent_seconds_from_issue(fields: dict) -> int | None:
    """Logged / time spent in seconds.

    Prefers aggregatetimespent (all-time rollup including subtasks) when present,
    then timetracking / timespent.
    """
    agg = fields.get("aggregatetimespent")
    if agg is not None:
        try:
            return int(agg)
        except (TypeError, ValueError):
            pass
    return _seconds_from(
        fields,
        tt_keys=("timeSpentSeconds", "timeSpentInSeconds"),
        field_key="timespent",
    )


def remaining_hours(seconds: int | None) -> float:
    if seconds is None:
        return 0.0
    return round(seconds / 3600.0, 2)


def hours_or_none(seconds: int | None) -> float | None:
    if seconds is None:
        return None
    return round(seconds / 3600.0, 2)


def _component_names(fields: dict) -> list[str]:
    comps = fields.get("components") or []
    names = []
    for c in comps:
        if isinstance(c, dict) and c.get("name"):
            names.append(str(c["name"]))
        elif isinstance(c, str):
            names.append(c)
    return names


def _assignee_display(assignee: dict) -> str | None:
    if not isinstance(assignee, dict):
        return None
    return (
        assignee.get("displayName")
        or assignee.get("emailAddress")
        or assignee.get("accountId")
    )


def _status_category(fields: dict) -> str | None:
    status = fields.get("status") or {}
    if not isinstance(status, dict):
        return None
    cat = status.get("statusCategory") or {}
    if isinstance(cat, dict):
        return cat.get("name") or cat.get("key")
    return None


def _parse_status_entries(changelog: dict | None) -> list[dict]:
    """Extract status transition events: [{to, at}] chronologically."""
    if not changelog:
        return []
    entries: list[dict] = []
    for hist in changelog.get("histories") or []:
        created = hist.get("created")
        for item in hist.get("items") or []:
            if (item.get("field") or "").lower() != "status":
                continue
            entries.append(
                {
                    "from": item.get("fromString"),
                    "to": item.get("toString"),
                    "at": created,
                }
            )
    entries.sort(key=lambda e: e.get("at") or "")
    return entries


def parse_jira_datetime(raw: Any) -> datetime | None:
    """Parse a Jira ISO timestamp (with or without colon in the tz offset)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # +0200 → +02:00
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:19])
        except ValueError:
            return None


def parse_jira_date(raw: Any) -> date | None:
    dt = parse_jira_datetime(raw)
    if dt is not None:
        return dt.date()
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def first_entered_status_at(changelog: dict | None, status_name: str) -> date | None:
    """Date the issue first entered `status_name` (working from changelog)."""
    want = (status_name or "").strip().lower()
    if not want:
        return None
    for e in _parse_status_entries(changelog):
        if (e.get("to") or "").strip().lower() == want:
            parsed = parse_jira_date(e.get("at"))
            if parsed:
                return parsed
    return None


def last_status_change_date(raw: dict | None) -> date | None:
    """Most recent status transition, else issue created date."""
    if not raw:
        return None
    last: date | None = None
    for e in raw.get("status_entries") or []:
        parsed = parse_jira_date(e.get("at") if isinstance(e, dict) else None)
        if parsed and (last is None or parsed > last):
            last = parsed
    if last is not None:
        return last
    return parse_jira_date(raw.get("created"))


def status_unchanged_days(raw: dict | None, report_date: date) -> int | None:
    last = last_status_change_date(raw)
    if last is None:
        return None
    return max(0, (report_date - last).days)


def issue_to_document(issue: dict, *, event_date: date | None = None) -> Document:
    """Map a Jira Cloud issue JSON object to a Document."""
    fields = issue.get("fields") or {}
    key = issue.get("key") or issue.get("id") or ""
    assignee = fields.get("assignee") or {}
    account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    email = (assignee.get("emailAddress") or "").lower() if isinstance(assignee, dict) else ""
    display = _assignee_display(assignee) if isinstance(assignee, dict) else None
    identity = account_id or email
    status = fields.get("status") or {}
    status_name = status.get("name") if isinstance(status, dict) else str(status or "")
    project = fields.get("project") or {}
    project_key = project.get("key") if isinstance(project, dict) else None
    project_name = project.get("name") if isinstance(project, dict) else None
    issuetype = fields.get("issuetype") or {}
    type_name = issuetype.get("name") if isinstance(issuetype, dict) else None
    summary = fields.get("summary") or key
    rem_s = remaining_seconds_from_issue(fields)
    orig_s = original_seconds_from_issue(fields)
    spent_s = spent_seconds_from_issue(fields)
    components = _component_names(fields)
    parent = fields.get("parent") or {}
    parent_key = parent.get("key") if isinstance(parent, dict) else None
    changelog = issue.get("changelog")
    status_entries = _parse_status_entries(changelog)
    base = (get_settings().jira_base_url or "").rstrip("/")
    url = f"{base}/browse/{key}" if base and key else None
    body_parts = [f"Status: {status_name}"]
    if orig_s is not None:
        body_parts.append(f"Planned: {remaining_hours(orig_s)}h")
    if spent_s is not None:
        body_parts.append(f"Logged: {remaining_hours(spent_s)}h")
    if rem_s is not None:
        body_parts.append(f"Remaining: {remaining_hours(rem_s)}h")
    if fields.get("duedate"):
        body_parts.append(f"Due: {fields['duedate']}")
    comment_field = fields.get("comment") or {}
    comments = []
    if isinstance(comment_field, dict):
        for c in (comment_field.get("comments") or [])[-5:]:
            author = ((c.get("author") or {}).get("displayName")) if isinstance(c, dict) else None
            comments.append(
                {
                    "author": author,
                    "created": c.get("created") if isinstance(c, dict) else None,
                    "body": str(c.get("body") or "")[:500] if isinstance(c, dict) else "",
                }
            )
    return Document(
        source="jira",
        external_id=str(key),
        event_date=event_date or date.today(),
        author_identity=identity or "",
        title=f"{key}: {summary}" if key else str(summary),
        body=" · ".join(body_parts),
        url=url,
        jira_issue_type=type_name,
        project_hint=project_key,
        raw={
            "id": str(issue.get("id") or "") or None,
            "key": key,
            "summary": summary,
            "status": status_name,
            "status_category": _status_category(fields),
            "assignee_account_id": account_id,
            "assignee_email": email or None,
            "assignee_display": display,
            "original_seconds": orig_s,
            "original_hours": hours_or_none(orig_s),
            "spent_seconds": spent_s,
            "spent_hours": hours_or_none(spent_s),
            "remaining_seconds": rem_s,
            "remaining_hours": hours_or_none(rem_s) if rem_s is not None else 0.0,
            "duedate": fields.get("duedate"),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "project_key": project_key,
            "project_name": project_name,
            "issue_type": type_name,
            "components": components,
            "parent_key": parent_key,
            "status_entries": status_entries,
            "comments": comments,
        },
    )


class JiraClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        if not self.s.jira_configured:
            raise RuntimeError(
                "Jira not configured — set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."
            )
        self.base = self.s.jira_base_url.rstrip("/")
        self._auth = (self.s.jira_email, self.s.jira_api_token)
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base,
            auth=self._auth,
            headers=self._headers,
            timeout=60.0,
        )

    def myself(self) -> dict:
        with self._client() as c:
            r = c.get("/rest/api/3/myself")
            r.raise_for_status()
            return r.json()

    def resolve_account_id(self, email: str) -> str | None:
        """Look up Atlassian accountId by email. None if not found / no permission."""
        user = self.lookup_user(email)
        return (user or {}).get("accountId")

    def lookup_user(self, email: str) -> dict | None:
        """Return Jira user dict for an exact email match, else None."""
        if not email:
            return None
        with self._client() as c:
            r = c.get("/rest/api/3/user/search", params={"query": email})
            r.raise_for_status()
            users = r.json() or []
        email_l = email.lower()
        for u in users:
            if (u.get("emailAddress") or "").lower() == email_l:
                return u
        if len(users) == 1 and (users[0].get("emailAddress") or "").lower() in ("", email_l):
            return users[0]
        return None

    def search_projects(self, query: str, *, max_results: int = 20) -> list[dict]:
        """Search Jira Cloud projects by name/key. Returns [{key, name, id}, ...]."""
        q = (query or "").strip()
        if not q:
            return []
        with self._client() as c:
            r = c.get(
                "/rest/api/3/project/search",
                params={"query": q, "maxResults": max_results, "orderBy": "name"},
            )
            r.raise_for_status()
            data = r.json() or {}
        rows = data.get("values") or data.get("projects") or []
        out: list[dict] = []
        for p in rows:
            if not isinstance(p, dict) or not p.get("key"):
                continue
            out.append(
                {
                    "key": str(p["key"]).upper(),
                    "name": (p.get("name") or "").strip() or str(p["key"]),
                    "id": str(p.get("id") or ""),
                }
            )
        return out

    def get_project(self, key: str) -> dict | None:
        """Fetch one Jira project by key; None if missing."""
        k = (key or "").strip().upper()
        if not k:
            return None
        with self._client() as c:
            r = c.get(f"/rest/api/3/project/{k}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            p = r.json() or {}
        if not p.get("key"):
            return None
        return {
            "key": str(p["key"]).upper(),
            "name": (p.get("name") or "").strip() or str(p["key"]),
            "id": str(p.get("id") or ""),
        }

    def search_jql(
        self,
        jql: str,
        *,
        max_results: int = 100,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Paginate Jira issue search via enhanced /rest/api/3/search/jql.

        Classic /rest/api/3/search returns 410 on current Jira Cloud (CHANGE-2046).
        Enhanced API paginates with nextPageToken + isLast (no startAt/total).
        Optional ``limit`` stops after that many issues (for lightweight sampling).
        """
        issues: list[dict] = []
        next_page_token: str | None = None
        field_list = fields or _ISSUE_FIELDS
        page_size = max_results
        if limit is not None:
            page_size = max(1, min(max_results, limit))
        with self._client() as c:
            while True:
                payload: dict[str, Any] = {
                    "jql": jql,
                    "fields": field_list,
                    "maxResults": page_size,
                }
                if expand:
                    payload["expand"] = ",".join(expand)
                if next_page_token:
                    payload["nextPageToken"] = next_page_token
                r = c.post("/rest/api/3/search/jql", json=payload)
                if r.status_code == 404:
                    classic: dict[str, Any] = {
                        "jql": jql,
                        "fields": field_list,
                        "maxResults": page_size,
                        "startAt": len(issues),
                    }
                    if expand:
                        classic["expand"] = expand
                    r = c.post("/rest/api/3/search", json=classic)
                r.raise_for_status()
                data = r.json()
                batch = data.get("issues") or []
                issues.extend(batch)
                if limit is not None and len(issues) >= limit:
                    return issues[:limit]
                if not batch:
                    break
                total = data.get("total")
                if total is not None and len(issues) >= total:
                    break
                if data.get("isLast") is True:
                    break
                next_page_token = data.get("nextPageToken")
                if next_page_token:
                    if len(issues) > 2000:
                        break
                    continue
                if total is None:
                    break
                if len(issues) > 2000:
                    break
        return issues

    def search_open_assigned(
        self,
        assignee_account_ids: list[str],
        *,
        window_from: date | None = None,
        window_to: date | None = None,
    ) -> list[Document]:
        """Open (non-Done) issues assigned to the given accountIds.

        Excludes time-log bucket types and IMR (hardware) project (§5).
        """
        ids = [a for a in assignee_account_ids if a]
        if not ids:
            return []
        id_list = ", ".join(f'"{i}"' for i in ids)
        type_exclusions = ", ".join(
            f'"{t}"' for t in sorted(TIME_LOG_BUCKET_TYPES | {"Epic"})
        )
        jql = (
            f"assignee in ({id_list}) "
            f"AND statusCategory != Done "
            f"AND issuetype not in ({type_exclusions}) "
            f'AND project != "IMR" '
            f'AND issuetype != "In Use" '
            f"ORDER BY assignee ASC, updated DESC"
        )
        _ = (window_from, window_to)
        event_date = window_from or date.today()
        return [
            issue_to_document(issue, event_date=event_date)
            for issue in self.search_jql(jql)
        ]

    def list_issue_worklogs(self, issue_key: str) -> list[dict]:
        """All worklogs on an issue (paginated)."""
        key = (issue_key or "").strip()
        if not key:
            return []
        out: list[dict] = []
        start_at = 0
        with self._client() as c:
            while True:
                r = c.get(
                    f"/rest/api/3/issue/{key}/worklog",
                    params={"startAt": start_at, "maxResults": 1000},
                )
                r.raise_for_status()
                data = r.json() or {}
                batch = data.get("worklogs") or []
                out.extend(batch)
                total = int(data.get("total") or 0)
                start_at += len(batch)
                if not batch or start_at >= total:
                    break
        return out

    def search_worklogged_on(
        self,
        author_account_ids: list[str],
        worklog_date: date,
    ) -> list[dict]:
        """Issues someone in ``author_account_ids`` logged time against on ``worklog_date``.

        Returns one dict per (author, issue): document + hours that day.
        Excludes time-log buckets, epics, IMR, and In Use types.
        """
        ids = [a for a in author_account_ids if a]
        if not ids:
            return []
        id_list = ", ".join(f'"{i}"' for i in ids)
        id_set = set(ids)
        type_exclusions = ", ".join(
            f'"{t}"' for t in sorted(TIME_LOG_BUCKET_TYPES | {"Epic"})
        )
        day = worklog_date.isoformat()
        jql = (
            f'worklogDate = "{day}" '
            f"AND worklogAuthor in ({id_list}) "
            f"AND issuetype not in ({type_exclusions}) "
            f'AND project != "IMR" '
            f'AND issuetype != "In Use" '
            f"ORDER BY updated DESC"
        )
        issues = self.search_jql(
            jql,
            fields=list(_ISSUE_FIELDS) + ["worklog"],
            expand=_HEALTH_EXPAND,
        )
        rows: list[dict] = []
        for issue in issues:
            doc = issue_to_document(issue, event_date=worklog_date)
            fields = issue.get("fields") or {}
            wl = fields.get("worklog") or {}
            worklogs = list(wl.get("worklogs") or [])
            total = int(wl.get("total") or 0) if isinstance(wl, dict) else 0
            if total > len(worklogs) or not worklogs:
                try:
                    worklogs = self.list_issue_worklogs(doc.external_id)
                except Exception:  # noqa: BLE001 — still emit the issue with 0h
                    pass
            hours_by_author: dict[str, float] = {}
            for w in worklogs:
                if not isinstance(w, dict):
                    continue
                author = w.get("author") or {}
                aid = author.get("accountId") if isinstance(author, dict) else None
                if not aid or aid not in id_set:
                    continue
                started = parse_jira_date(w.get("started"))
                if started != worklog_date:
                    continue
                try:
                    secs = int(w.get("timeSpentSeconds") or 0)
                except (TypeError, ValueError):
                    secs = 0
                if secs <= 0:
                    continue
                hours_by_author[aid] = hours_by_author.get(aid, 0.0) + secs / 3600.0
            if not hours_by_author:
                continue
            stale = status_unchanged_days(doc.raw, worklog_date)
            for aid, hours in hours_by_author.items():
                rows.append(
                    {
                        "document": doc,
                        "author_account_id": aid,
                        "hours": round(hours, 2),
                        "status_unchanged_days": stale,
                    }
                )
        return rows

    def search_by_keys(
        self,
        keys: list[str],
        *,
        event_date: date | None = None,
        expand: list[str] | None = None,
    ) -> list[Document]:
        """Fetch specific issues by key (Friday-planned set). Chunks to keep JQL short."""
        clean = sorted({k.strip().upper() for k in keys if k and k.strip()})
        if not clean:
            return []
        when = event_date or date.today()
        docs: list[Document] = []
        chunk_size = 50
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            key_list = ", ".join(chunk)
            jql = f"key in ({key_list})"
            docs.extend(
                issue_to_document(issue, event_date=when)
                for issue in self.search_jql(jql, expand=expand)
            )
        return docs

    def search_by_ids(
        self,
        ids: list[str],
        *,
        event_date: date | None = None,
        expand: list[str] | None = None,
    ) -> list[Document]:
        """Fetch issues by numeric Jira id (Tempo worklogs expose id, not key)."""
        clean = sorted({str(i).strip() for i in ids if i and str(i).strip()})
        if not clean:
            return []
        when = event_date or date.today()
        docs: list[Document] = []
        chunk_size = 50
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            jql = f"id in ({', '.join(chunk)})"
            docs.extend(
                issue_to_document(issue, event_date=when)
                for issue in self.search_jql(jql, expand=expand)
            )
        return docs

    def search_project_issues(
        self,
        project_key: str,
        *,
        event_date: date | None = None,
        with_changelog: bool = True,
    ) -> list[Document]:
        """Full-history issues for a Jira project (A2 health burn totals).

        Do NOT window by updated date — windowed pulls understate logged/estimate.
        """
        key = (project_key or "").strip().upper()
        if not key:
            return []
        jql = f'project = "{key}" ORDER BY key ASC'
        expand = _HEALTH_EXPAND if with_changelog else None
        when = event_date or date.today()
        return [
            issue_to_document(issue, event_date=when)
            for issue in self.search_jql(jql, expand=expand)
        ]


def search_issues(
    jql: str,
    window_from: date,
    window_to: date,
    *,
    settings: Settings | None = None,
) -> list[Document]:
    """Public adapter entry — run arbitrary JQL."""
    _ = window_to
    client = JiraClient(settings)
    return [
        issue_to_document(i, event_date=window_from) for i in client.search_jql(jql)
    ]


def resolve_roster_account_ids(
    people: list[Any],
    *,
    persist: bool = True,
    settings: Settings | None = None,
) -> dict[str, str]:
    """Ensure each person has a jira_account_id; return {person_id_str: accountId}."""
    client = JiraClient(settings)
    out: dict[str, str] = {}
    for p in people:
        pid = str(getattr(p, "id", ""))
        existing = getattr(p, "jira_account_id", None)
        if existing:
            out[pid] = existing
            continue
        emails = list(getattr(p, "emails", None) or [])
        resolved = None
        for email in emails:
            resolved = client.resolve_account_id(email)
            if resolved:
                break
        if resolved:
            out[pid] = resolved
            if persist and hasattr(p, "jira_account_id"):
                p.jira_account_id = resolved
    return out
