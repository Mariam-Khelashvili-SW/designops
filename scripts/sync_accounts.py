"""Sync the Fairwind account directory into the local `account` table (§10.1, §11.1).

GET /accounts (cursor-paginated) + /jira-projects → upsert `Account` rows on
`fairwind_account_id`. Sync owns only the directory columns (name, domains, jira
keys, salesforce/notion ids, data_availability). It NEVER touches the local columns
(digest_enabled / aliases / enabled_by / enabled_at / notes) and NEVER deletes — a
sync that clobbers `digest_enabled` is the one bug that makes Olga stop trusting the
digest (§11.1).

Manual for now (no scheduler): `python -m scripts.sync_accounts`.
"""

from __future__ import annotations

import json

from designops.adapters.fairwind import FairwindClient
from designops.core.config import get_settings
from designops.core.db import session_scope
from designops.core.models import Account


def _keys(items, *names) -> list[str]:
    """Pull the first present key from each dict item (tolerant of shape)."""
    out: list[str] = []
    for it in items or []:
        if isinstance(it, dict):
            for n in names:
                if it.get(n):
                    out.append(str(it[n]))
                    break
        elif isinstance(it, str):
            out.append(it)
    return out


def sync_accounts() -> dict:
    settings = get_settings()
    if not settings.fairwind_configured:
        raise SystemExit("FW_CLIENT_ID / FW_CLIENT_SECRET not set — load them from env (§10).")

    client = FairwindClient(settings)
    accounts = client.list_accounts()
    jira_projects = client.list_jira_projects()

    created = updated = 0
    with session_scope() as s:
        existing = {a.fairwind_account_id: a for a in s.query(Account).all()}
        for r in accounts:
            fid = r.get("id")
            if not fid:
                continue
            row = existing.get(fid)
            if row is None:
                row = Account(fairwind_account_id=fid, name=r.get("name") or "(unnamed)")
                s.add(row)
                created += 1
            else:
                updated += 1
            # SYNC-OWNED columns only — local columns are never touched here (§11.1)
            row.name = r.get("name") or row.name
            row.is_active = bool(r.get("is_active"))
            row.domains = r.get("domains") or []
            row.jira_project_keys = _keys(r.get("jira_projects"), "key")
            row.salesforce_account_ids = _keys(r.get("salesforce_accounts"), "sf_id", "id")
            row.notion_space_ids = _keys(r.get("notion_spaces"), "id", "space_id")
            row.data_availability = r.get("data_availability") or {}
        s.flush()  # count the pending upserts, not the pre-sync state
        total = s.query(Account).count()
        enabled = s.query(Account).filter_by(digest_enabled=True).count()

    return {
        "fetched": len(accounts),
        "jira_projects": len(jira_projects),
        "created": created,
        "updated": updated,
        "total_in_db": total,
        "digest_enabled": enabled,
    }


if __name__ == "__main__":
    print(json.dumps(sync_accounts(), indent=2))
