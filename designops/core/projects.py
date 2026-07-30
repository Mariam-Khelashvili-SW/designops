"""Keep the project registry in step with the enabled accounts.

Enabling an account must never leave its work looking "untracked" in the digest just
because no `project` row maps to it. `ensure_project_for_account` creates (or links) a
matching project so a designer's mention of that project resolves to the enabled account.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from designops.core.models import Account, Project


def jira_project_keys_from_docs(docs: list) -> set[str]:
    """Unique Jira project keys from Documents (project_hint / raw / issue key prefix)."""
    keys: set[str] = set()
    for doc in docs:
        hint = (getattr(doc, "project_hint", None) or "").strip().upper()
        if hint:
            keys.add(hint)
        raw = getattr(doc, "raw", None) or {}
        pk = (raw.get("project_key") or "").strip().upper()
        if pk:
            keys.add(pk)
        issue = (raw.get("key") or getattr(doc, "external_id", None) or "").strip().upper()
        if "-" in issue:
            keys.add(issue.rsplit("-", 1)[0])
    return keys


def enable_accounts_for_jira_keys(
    session: Session,
    jira_keys: set[str],
    *,
    enabled_by: str | None = None,
) -> list[Account]:
    """Turn on digest_enabled for Fairwind accounts that match the given Jira keys.

    Matches Account.jira_project_keys and Project.jira_project_key → fairwind_account_id.
    Already-enabled accounts are left alone. Newly enabled ones get a project row linked.
    """
    wanted = {k.strip().upper() for k in jira_keys if k and str(k).strip()}
    if not wanted:
        return []

    to_enable: dict[str, Account] = {}

    for acct in session.query(Account).all():
        if not acct.fairwind_account_id:
            continue
        acct_keys = {str(k).strip().upper() for k in (acct.jira_project_keys or []) if k}
        if acct_keys & wanted:
            to_enable[acct.fairwind_account_id] = acct

    for proj in session.query(Project).filter(Project.jira_project_key.isnot(None)).all():
        pk = (proj.jira_project_key or "").strip().upper()
        fw = (proj.fairwind_account_id or "").strip()
        if not pk or not fw or pk not in wanted:
            continue
        if fw in to_enable:
            continue
        acct = session.query(Account).filter_by(fairwind_account_id=fw).one_or_none()
        if acct:
            to_enable[fw] = acct

    newly: list[Account] = []
    now = datetime.now()
    for acct in to_enable.values():
        if acct.digest_enabled:
            continue
        acct.digest_enabled = True
        acct.enabled_by = enabled_by
        acct.enabled_at = now
        session.add(acct)
        ensure_project_for_account(session, acct)
        newly.append(acct)
    if newly:
        session.flush()
    return newly


def ensure_project_for_account(session: Session, account: Account) -> Project:
    """Return the project mapped to this account, creating or linking one if needed."""
    # already linked by account id
    existing = (
        session.query(Project)
        .filter_by(fairwind_account_id=account.fairwind_account_id)
        .first()
    )
    if existing:
        return existing
    # a project with the same name exists but isn't linked yet — link it
    by_name = (
        session.query(Project).filter(Project.canonical_name == account.name).one_or_none()
    )
    if by_name:
        by_name.fairwind_account_id = by_name.fairwind_account_id or account.fairwind_account_id
        session.add(by_name)
        return by_name
    # otherwise create a fresh project for the account
    proj = Project(
        canonical_name=account.name,
        aliases=[account.name],
        fairwind_account_id=account.fairwind_account_id,
        active=True,
        track_daily=True,
        notes=f"Auto-created when '{account.name}' was enabled for the daily report.",
    )
    session.add(proj)
    return proj
