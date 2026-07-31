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
        acct_keys = {str(k).strip().upper() for k in (acct.jira_project_keys or []) if k}
        matched = sorted(acct_keys & wanted)
        if not matched:
            # matched via Project.jira_project_key → fairwind_account_id
            matched = sorted(
                {
                    (p.jira_project_key or "").strip().upper()
                    for p in session.query(Project)
                    .filter_by(fairwind_account_id=acct.fairwind_account_id)
                    .all()
                    if (p.jira_project_key or "").strip().upper() in wanted
                }
            )
        acct.digest_enabled = True
        acct.enabled_by = enabled_by
        acct.enabled_at = now
        keys_txt = ", ".join(matched) if matched else "matching Jira project"
        acct.notes = (
            f"Auto-enabled from weekly backlog — design team had Jira work on {keys_txt}."
        )
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
        _apply_account_jira_keys(existing, account)
        session.add(existing)
        return existing
    # a project with the same name exists but isn't linked yet — link it
    by_name = (
        session.query(Project).filter(Project.canonical_name == account.name).one_or_none()
    )
    if by_name:
        by_name.fairwind_account_id = by_name.fairwind_account_id or account.fairwind_account_id
        _apply_account_jira_keys(by_name, account)
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
    _apply_account_jira_keys(proj, account)
    session.add(proj)
    return proj


def _apply_account_jira_keys(project: Project, account: Account) -> bool:
    """Copy Account.jira_project_keys onto Project.jira_project_key when missing.

    Returns True if the project key was set/updated from the account.
    """
    keys = [
        str(k).strip().upper()
        for k in (account.jira_project_keys or [])
        if k and str(k).strip()
    ]
    if not keys:
        return False
    current = (project.jira_project_key or "").strip().upper()
    if current:
        return False
    project.jira_project_key = keys[0]
    return True


def resolve_jira_candidates(
    *,
    project_name: str,
    fairwind_account_id: str | None,
    account_keys: list[str] | None = None,
    fairwind_jira_projects: list[dict] | None = None,
    jira_cloud_projects: list[dict] | None = None,
) -> list[dict]:
    """Rank possible Jira project keys for a health-tracked project.

    Each candidate: {key, name, source, score} where lower score = better.
    """
    name = (project_name or "").strip()
    nl = name.lower()
    fid = (fairwind_account_id or "").strip()
    by_key: dict[str, dict] = {}

    def _add(key: str, pname: str | None, source: str, score: int) -> None:
        k = (key or "").strip().upper()
        if not k:
            return
        row = by_key.get(k)
        if row is None or score < row["score"]:
            by_key[k] = {
                "key": k,
                "name": (pname or "").strip() or k,
                "source": source,
                "score": score,
            }

    for k in account_keys or []:
        kk = str(k).strip().upper()
        if not kk:
            continue
        score = 1
        if nl and (kk.lower() in nl or nl.startswith(kk.lower())):
            score = 0
        _add(kk, name, "fairwind_account", score)

    for r in fairwind_jira_projects or []:
        key = str(r.get("key") or "").strip().upper()
        if not key:
            continue
        pname = (r.get("name") or "").strip()
        acct = str(r.get("account") or r.get("account_id") or "").strip()
        if fid and acct and acct == fid:
            _add(key, pname or name, "fairwind_map", 0)
            continue
        pl = pname.lower()
        if nl and pl and (nl == pl or nl in pl or pl in nl):
            _add(key, pname, "fairwind_map", 2 if nl != pl else 0)

    for r in jira_cloud_projects or []:
        key = str(r.get("key") or "").strip().upper()
        if not key:
            continue
        pname = (r.get("name") or "").strip()
        pl = pname.lower()
        if nl and pl == nl:
            _add(key, pname, "jira", 0)
        elif nl and pl and (nl in pl or pl in nl or key.lower() in nl):
            _add(key, pname, "jira", 2)
        else:
            _add(key, pname, "jira", 3)

    return sorted(by_key.values(), key=lambda m: (m["score"], m["key"]))
