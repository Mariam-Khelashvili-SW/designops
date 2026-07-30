"""Normalized Document — every adapter returns this shape (§4).

`person_id` / `project_id` are RESOLVED fields: they start None and are filled by the
deterministic filter (`pipelines.filter`). The identity/date/type fields the filter
reads (`author_identity`, `event_date`, `jira_issue_type`, `project_hint`,
`message_id`) are populated by the adapter from the raw source.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class Document:
    source: str  # gmail | jira | fairwind | transcript
    external_id: str
    event_date: date  # the date scope filtering uses
    author_identity: str  # email (lowercased) or jira accountId
    title: str
    body: str  # verbatim, NEVER pre-summarised (§4)
    url: str | None = None
    raw: dict = field(default_factory=dict)

    # ingestion metadata the filter reads
    message_id: str | None = None  # RFC-822 Message-ID for dedup (§11.2)
    sent_at: datetime | None = None  # dedup fallback component
    jira_issue_type: str | None = None  # for TIME_LOG_BUCKET_TYPES check
    project_hint: str | None = None  # raw project string to resolve against the registry
    account_id: str | None = None  # Fairwind export envelope this copy arrived in

    # resolved by the filter — None until then
    person_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    def dedup_key(self) -> str:
        """Collapse copies of the same item that arrive under multiple account exports.

        Emails (the §11.2 multi-account case): RFC-822 Message-ID, else
        sha256(from + sent_at + normalized_body). Jira/transcripts have a stable
        per-item id, so they key on (source, external_id) per §6.1 — two distinct
        tickets never collide just because their bodies look alike.
        """
        if self.source in ("jira", "transcript"):
            return f"{self.source}:{self.external_id}"
        if self.message_id:
            return f"mid:{self.message_id.strip().lower()}"
        norm_body = " ".join(self.body.split()).lower()
        sent = self.sent_at.isoformat() if self.sent_at else ""
        digest = hashlib.sha256(
            f"{self.author_identity.lower()}|{sent}|{norm_body}".encode()
        ).hexdigest()
        return f"sha:{digest}"

    # --- (de)serialization for a persisted corpus (§12.2) -------------------
    def to_record(self) -> dict:
        """JSON-safe dict for a reusable corpus. `person_id`/`project_id` are omitted
        on purpose — those are resolved fresh by the filter each run."""
        return {
            "source": self.source,
            "external_id": self.external_id,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "author_identity": self.author_identity,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "message_id": self.message_id,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "jira_issue_type": self.jira_issue_type,
            "project_hint": self.project_hint,
            "account_id": self.account_id,
            "raw": self.raw,
        }

    @classmethod
    def from_record(cls, r: dict) -> Document:
        def _d(v):
            return date.fromisoformat(v) if v else date.min

        def _dt(v):
            return datetime.fromisoformat(v) if v else None

        return cls(
            source=r["source"],
            external_id=r["external_id"],
            event_date=_d(r.get("event_date")),
            author_identity=r.get("author_identity", ""),
            title=r.get("title", ""),
            body=r.get("body", ""),
            url=r.get("url"),
            message_id=r.get("message_id"),
            sent_at=_dt(r.get("sent_at")),
            jira_issue_type=r.get("jira_issue_type"),
            project_hint=r.get("project_hint"),
            account_id=r.get("account_id"),
            raw=r.get("raw") or {},
        )
