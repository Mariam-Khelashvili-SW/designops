"""Fairwind export adapter (§6.1, §10, §11.3).

Async, per-account, 409-locked, 24h-expiring exports. Used for the daily corpus in
v1 (source_mode=fairwind) and for registry sync (§10.1).

Two entry points:
  * `FairwindClient` — live REST: OAuth client-credentials token (cached, refreshed
    on 401 / at T-5min), create → poll → download, fan-out with bounded concurrency
    and 409 backoff, persist raw payloads to CORPUS_STORE_DIR.
  * `load_fixture_corpus(path)` — offline: read a persisted/fixture corpus JSON into
    `Document`s so the filter and golden tests run with no network (dev + CI).

Secrets come from Settings (env) only — never DB, never git (§10).
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

from designops.adapters.documents import Document
from designops.core.config import Settings, get_settings


class FairwindError(RuntimeError):
    pass


@dataclass
class _Token:
    value: str
    expires_at: float  # epoch seconds


class FairwindClient:
    def __init__(self, settings: Settings | None = None, *, clock=time.time):
        self.s = settings or get_settings()
        if not self.s.fairwind_configured:
            raise FairwindError(
                "FW_CLIENT_ID / FW_CLIENT_SECRET not set. Rotate the leaked secret (§9.6) "
                "and load the new pair from env."
            )
        self._clock = clock
        self._token: _Token | None = None
        self._client = httpx.Client(base_url=self.s.fw_base_url, timeout=60)

    # --- auth ---------------------------------------------------------------
    def _get_token(self) -> str:
        now = self._clock()
        if self._token and self._token.expires_at - 300 > now:  # refresh at T-5min
            return self._token.value
        r = self._client.post(
            "/api/auth/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.s.fw_client_id,
                "client_secret": self.s.fw_client_secret,
                "resource": self.s.fw_resource,  # audience-binds the JWT (§10)
                "scope": self.s.fw_scope,
            },
        )
        r.raise_for_status()
        data = r.json()
        self._token = _Token(
            value=data["access_token"], expires_at=now + int(data.get("expires_in", 3600))
        )
        return self._token.value

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _get(self, url: str, **kw) -> httpx.Response:
        r = self._client.get(url, headers=self._headers(), **kw)
        if r.status_code == 401:  # token expired mid-flight — refresh once
            self._token = None
            r = self._client.get(url, headers=self._headers(), **kw)
        return r

    # --- exports ------------------------------------------------------------
    def _post_export(
        self, account_id: str, date_from: date, date_to: date, data_types: list[str]
    ) -> str:
        """POST /exports → export id.

        On 409 (an export for this account is already in flight) the spec is explicit
        that this is reuse/backoff, never failure (§6.1, §11.3). We first try to reuse
        the in-flight export id if the 409 body names it; otherwise we back off and
        retry until the lock clears."""
        payload = {
            "account_id": account_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "data_types": data_types,
            "include_files": False,  # ALWAYS false (§10.2)
        }
        for attempt in range(6):
            r = self._client.post("/api/v1/exports", headers=self._headers(), json=payload)
            if r.status_code != 409:
                r.raise_for_status()
                return r.json()["id"]
            try:
                body = r.json()
            except ValueError:
                body = r.text
            existing = _extract_export_id(body)
            if existing:  # reuse the in-flight export rather than waiting it out
                return existing
            ra = r.headers.get("Retry-After")
            delay = int(ra) if (ra and ra.isdigit()) else min(2 ** attempt, 30)
            time.sleep(delay)
        raise FairwindError(f"export for {account_id} stuck at 409 (already in flight)")

    def create_export(
        self, account_id: str, report_date: date, *, data_types: list[str] | None = None
    ) -> str:
        """Single-day export (the daily corpus path). data_types defaults to
        settings.fw_data_types — v1 pulls jira + transcripts, no Fairwind emails."""
        types = data_types if data_types is not None else self.s.fw_data_types
        return self._post_export(account_id, report_date, report_date, types)

    def create_export_range(
        self,
        account_id: str,
        date_from: date,
        date_to: date,
        *,
        data_types: list[str] | None = None,
    ) -> str:
        """Arbitrary date-range export (the on-demand explorer / backfill path, §10.2)."""
        types = data_types if data_types is not None else self.s.fw_data_types
        return self._post_export(account_id, date_from, date_to, types)

    def poll_export(self, export_id: str, *, timeout_s: int = 300, interval_s: int = 5) -> dict:
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            r = self._get(f"/api/v1/exports/{export_id}")
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
            if status == "ready":
                return data
            if status == "failed":
                raise FairwindError(f"export {export_id} failed")
            time.sleep(interval_s)
        raise FairwindError(f"export {export_id} not ready within {timeout_s}s")

    def download_manifest(self, export_id: str) -> dict:
        r = self._get(f"/api/v1/exports/{export_id}.json")
        r.raise_for_status()
        return r.json()

    def download_file(self, export_id: str, path: str) -> str:
        # Per-file streaming (`/files/{path}`) 502s unreliably at the edge — prefer the
        # whole-export `.zip` bundle (download_zip). Kept for targeted single-file reads.
        r = self._get(f"/api/v1/exports/{export_id}/files/{path}")
        r.raise_for_status()
        return r.text

    def download_zip(self, export_id: str) -> bytes:
        """The whole export as one zip (json/, markdown/, files/ trees). This is the
        reliable content path — single-file streaming 502s at the edge."""
        r = self._get(f"/api/v1/exports/{export_id}.zip")
        r.raise_for_status()
        return r.content

    def export_account(
        self,
        account_id: str,
        date_from: date,
        date_to: date,
        *,
        data_types: list[str] | None = None,
    ) -> dict:
        """On-demand: create → poll → download the zip bundle → extract → summarise an
        export for one account over a date range (§10.2 explorer / backfill). Synchronous —
        the caller waits for the poll loop. Counts come straight from the manifest (which
        is authoritative); content lands as extracted files under the store path."""
        types = data_types if data_types is not None else self.s.fw_data_types
        export_id = self.create_export_range(
            account_id, date_from, date_to, data_types=types
        )
        self.poll_export(export_id)
        manifest = self.download_manifest(export_id)
        blob = self.download_zip(export_id)

        label = f"{date_from.isoformat()}_{date_to.isoformat()}"
        root = Path(self.s.corpus_store_dir) / account_id / label
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (root / "export.zip").write_bytes(blob)
        file_count = _safe_extract_zip(blob, root / "files")

        counts = manifest.get("counts", {})
        return {
            "export_id": export_id,
            "account_id": account_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "data_types": types,
            "counts": counts,  # manifest counts: threads, jira_issues, transcripts, …
            "file_count": file_count,
            "zip_bytes": len(blob),
            "store_path": str(root),
        }

    # --- directory (§10.1) --------------------------------------------------
    def list_jira_projects(self) -> list[dict]:
        """Bulk {key, name, account} map — the registry seed. Unpaginated (§10.1)."""
        r = self._get("/api/v1/jira-projects")
        r.raise_for_status()
        return r.json().get("jira_projects", [])

    def list_accounts(self, *, page_limit: int = 200) -> list[dict]:
        """Full account directory, cursor-paginated to exhaustion (§10.1, §11.1)."""
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            r = self._get("/api/v1/accounts", params=params)
            r.raise_for_status()
            data = r.json()
            page = data.get("accounts") or data.get("data") or data.get("items") or []
            rows.extend(page)
            cursor = data.get("next_cursor")
            if not cursor or not page:
                break
        return rows

    # --- fan-out ------------------------------------------------------------
    def prepare_corpus(
        self,
        account_ids: list[str],
        report_date: date,
        *,
        concurrency: int = 3,
        data_types: list[str] | None = None,
        window_end: date | None = None,
    ) -> tuple[list[Document], dict]:
        """Fan out one export per account (bounded concurrency), union + persist.
        Returns (documents, coverage) where coverage records requested/succeeded/failed
        per §11.1 — one account failing is an ingest_gap, never a failed digest.

        The export spans report_date..window_end (default = report_date, single day). The
        daily digest passes window_end = report_date + 1 so a designer's daily written the
        next morning is captured; the deterministic filter then keeps client/Jira on the
        report day and internal dailies on the report day or next morning.

        Each account: create → poll → download the ZIP bundle → extract → parse the real
        thread/Jira tree (parse_export_dir). Uses the reliable zip path, never per-file
        streaming. data_types defaults to settings.fw_data_types."""
        end = window_end or report_date
        results: dict[str, list[Document]] = {}
        failed: list[str] = []
        per_account: dict[str, int] = {}

        def _one(account_id: str) -> None:
            try:
                export_id = self.create_export_range(
                    account_id, report_date, end, data_types=data_types
                )
                self.poll_export(export_id)
                manifest = self.download_manifest(export_id)
                blob = self.download_zip(export_id)
                root = Path(self.s.corpus_store_dir) / account_id / report_date.isoformat()
                root.mkdir(parents=True, exist_ok=True)
                (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
                (root / "export.zip").write_bytes(blob)
                _safe_extract_zip(blob, root / "files")
                docs = parse_export_dir(root / "files", account_id)
                results[account_id] = docs
                per_account[account_id] = len(docs)
            except Exception as exc:  # noqa: BLE001 — one account's failure is a gap, not fatal
                failed.append(account_id)
                per_account[account_id] = 0
                _log_account_failure(account_id, exc)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(_one, account_ids))

        documents = [d for docs in results.values() for d in docs]
        coverage = {
            "accounts_requested": len(account_ids),
            "exports_succeeded": len(results),
            "exports_failed": len(failed),
            "failed_accounts": failed,
            "docs_per_account": per_account,
            "data_types": data_types if data_types is not None else self.s.fw_data_types,
        }
        return documents, coverage


# --- 409 / failure helpers ---------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _extract_export_id(payload) -> str | None:
    """Dig an in-flight export id out of a 409 body of unknown shape (§6.1 reuse)."""
    if isinstance(payload, dict):
        for k in ("id", "export_id", "existing_export_id", "existing_id", "in_flight_id"):
            if payload.get(k):
                return str(payload[k])
        for k in ("export", "data", "detail", "error"):
            v = payload.get(k)
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"])
    text = payload if isinstance(payload, str) else json.dumps(payload)
    m = _UUID_RE.search(text)
    return m.group(0) if m else None


def _safe_extract_zip(blob: bytes, dest: Path) -> int:
    """Extract an export zip under `dest`, guarding against zip-slip (paths escaping the
    destination). Returns the number of files written."""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    written = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for member in z.infolist():
            if member.is_dir():
                continue
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(base)):  # zip-slip — skip
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, open(target, "wb") as out:
                out.write(src.read())
            written += 1
    return written


def _log_account_failure(account_id: str, exc: Exception) -> None:
    """One account failing is an ingest_gap, not a failed digest (§11.1) — but never
    swallow it silently. Surface the reason so a chronic gap is visible."""
    print(f"  ⚠ export failed for {account_id}: {type(exc).__name__}: {exc}")


# --- shared parsing / offline path -------------------------------------------

def parse_export_file(path: str, raw: str, account_id: str | None = None) -> list[Document]:
    """Turn one export file's JSON into Documents. Tolerant of shape vari: the export
    schema is validated empirically by §10.3 before this is trusted."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    records = data if isinstance(data, list) else data.get("items", [data])
    return [_record_to_document(r, path, account_id) for r in records if isinstance(r, dict)]


def _record_to_document(r: dict, path: str, account_id: str | None) -> Document:
    lower = path.lower()
    if "jira" in lower:
        source = "jira"
    elif "transcript" in lower:
        source = "transcript"
    else:
        source = "fairwind"
    ed = r.get("event_date") or r.get("date") or r.get("sent_at")
    event_date = _as_date(ed)
    if source == "jira":
        # Jira identity is the accountId, case-sensitive
        author = r.get("assignee_account_id") or r.get("author_identity") or ""
    else:
        author = (r.get("author_identity") or r.get("from") or "").lower()
    return Document(
        source=source,
        external_id=str(r.get("external_id") or r.get("id") or r.get("message_id") or path),
        event_date=event_date,
        author_identity=author,
        title=r.get("title") or r.get("subject") or "",
        body=r.get("body") or r.get("text") or "",
        url=r.get("url"),
        message_id=r.get("message_id"),
        sent_at=_as_datetime(r.get("sent_at")),
        jira_issue_type=r.get("issue_type") or r.get("jira_issue_type"),
        project_hint=r.get("project_hint") or r.get("project") or r.get("jira_project_key"),
        account_id=account_id or r.get("account_id"),
        raw=r,
    )


def _as_date(v) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    return date.min


def _as_datetime(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_QUOTE_ATTR = re.compile(
    r"(^On\b.*\bwrote:\s*$)|(-{3,}\s*Original Message)|(^From:\s)", re.I
)


def strip_quoted(body: str) -> str:
    """Drop the quoted reply history from an email body — daily-report threads append the
    whole prior chain (`> …`), and mining that resurfaces earlier days' projects under the
    wrong day/person. Keep only what the author wrote in THIS message."""
    lines = (body or "").split("\n")
    cut = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(">") or _QUOTE_ATTR.search(s):
            cut = i
            break
    if cut is None:
        return (body or "").strip()
    kept = lines[:cut]
    # trim the dangling attribution line just above the quote (has an <email>) + blanks
    while kept:
        last = kept[-1].strip()
        if not last or last.lower().endswith("wrote:") or ("@" in last and "<" in last):
            kept.pop()
        else:
            break
    return "\n".join(kept).strip()


def parse_export_dir(files_root: Path, account_id: str | None = None) -> list[Document]:
    """Parse an EXTRACTED Fairwind export tree into Documents (the real per-thread /
    per-issue schema, not the flat fixture shape).

    - `json/threads/**/*.json` → one Document per message. `author_identity` is the
      sender email (lowercased) so the roster filter matches it. The team's daily
      reports are the roster-authored messages in the internal threads.
    - `json/jira/**/*.json` → one Document per issue. `author_identity` is the assignee
      email; `jira_issue_type` drives the time-log-bucket drop; `project_hint` is the
      Jira project key (resolved via the registry).
    """
    files_root = Path(files_root)
    docs: list[Document] = []

    threads_root = files_root / "json" / "threads"
    if threads_root.exists():
        for p in sorted(threads_root.rglob("*.json")):
            folder = "internal" if "internal" in p.relative_to(threads_root).parts else "external"
            try:
                thread = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            subject = thread.get("subject") or ""
            thread_id = thread.get("thread_id")
            for m in thread.get("messages", []):
                if not isinstance(m, dict):
                    continue
                frm = m.get("from") or {}
                email = (frm.get("email") if isinstance(frm, dict) else frm) or ""
                mid = m.get("id")
                docs.append(
                    Document(
                        source="fairwind",
                        external_id=str(mid or f"{thread_id}:{m.get('sent_at')}"),
                        event_date=_as_date(m.get("sent_at")),
                        author_identity=str(email).lower(),
                        title=m.get("subject") or subject,
                        body=strip_quoted(m.get("body") or m.get("body_text") or ""),
                        message_id=str(mid) if mid is not None else None,
                        sent_at=_as_datetime(m.get("sent_at")),
                        project_hint=None,  # dailies group by project from the body (model's job)
                        account_id=account_id,
                        raw={"thread_id": thread_id, "folder": folder, "subject": subject},
                    )
                )

    jira_root = files_root / "json" / "jira"
    if jira_root.exists():
        for p in sorted(jira_root.rglob("*.json")):
            if p.name == "projects.json":
                continue
            try:
                issue = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            assignee = issue.get("assignee")
            email = assignee.get("email") if isinstance(assignee, dict) else assignee
            docs.append(
                Document(
                    source="jira",
                    external_id=str(issue.get("key") or p.stem),
                    event_date=_as_date(issue.get("updated") or issue.get("created")),
                    author_identity=str(email or "").lower(),
                    title=issue.get("summary") or "",
                    body=issue.get("description_text") or "",
                    jira_issue_type=issue.get("issue_type"),
                    project_hint=issue.get("project_key"),
                    account_id=account_id,
                    raw={"key": issue.get("key"), "status": issue.get("status")},
                )
            )

    transcripts_root = files_root / "json" / "transcripts"
    if transcripts_root.exists():
        for p in sorted(transcripts_root.rglob("*.json")):
            try:
                tr = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(tr, dict):
                continue
            title = (
                tr.get("title")
                or tr.get("name")
                or tr.get("subject")
                or p.stem
            )
            body = (
                tr.get("transcript_text")
                or tr.get("text")
                or tr.get("body")
                or tr.get("summary")
                or ""
            )
            ed = (
                tr.get("started_at")
                or tr.get("meeting_date")
                or tr.get("date")
                or tr.get("created_at")
            )
            docs.append(
                Document(
                    source="transcript",
                    external_id=str(tr.get("id") or p.stem),
                    event_date=_as_date(ed),
                    author_identity="",
                    title=str(title),
                    body=str(body),
                    account_id=account_id,
                    raw=tr,
                )
            )
    return docs


def load_fixture_corpus(path: str | Path) -> list[Document]:
    """Offline corpus loader for dev/CI/golden tests. Reads a single JSON file that is
    a list of raw records (same shape adapters emit). No network, no creds."""
    p = Path(path)
    data = json.loads(p.read_text())
    records = data if isinstance(data, list) else data.get("documents", [])
    return [
        _record_to_document(r, r.get("_path", "emails/internal"), r.get("account_id"))
        for r in records
    ]


# --- persisted corpus (§12.2 — ingest once, synthesize N times) --------------

def corpus_file(settings: Settings, report_date: date) -> Path:
    """One union corpus per report_date. Synthesis reads this; it never re-exports."""
    return Path(settings.corpus_store_dir) / report_date.isoformat() / "corpus.json"


def save_corpus(
    settings: Settings,
    report_date: date,
    documents: list[Document],
    account_ids: list[str] | None = None,
) -> Path:
    """Persist the full union (NOT deduped — the filter dedups + audits at synthesis,
    §11.2) so re-runs cost nothing and golden fixtures come free (§12.2). Records the
    account_ids the corpus covers, so reuse can tell when a newly-enabled account is
    missing and must be re-pulled."""
    path = corpus_file(settings, report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "report_date": report_date.isoformat(),
        "account_ids": account_ids or [],
        "documents": [d.to_record() for d in documents],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_corpus(settings: Settings, report_date: date) -> list[Document] | None:
    """Load a persisted corpus for the date, or None if none has been pulled yet."""
    path = corpus_file(settings, report_date)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return [Document.from_record(r) for r in data.get("documents", [])]


def corpus_account_ids(settings: Settings, report_date: date) -> list[str]:
    """Which accounts the persisted corpus for this date covers ([] if none/unknown)."""
    path = corpus_file(settings, report_date)
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("account_ids", [])
