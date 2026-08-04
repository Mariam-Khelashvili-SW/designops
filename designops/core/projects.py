"""Keep the project registry in step with the enabled accounts.

Enabling an account must never leave its work looking "untracked" in the digest just
because no `project` row maps to it. `ensure_project_for_account` creates (or links) a
matching project so a designer's mention of that project resolves to the enabled account.

Jira keys live on `Account.jira_project_keys` (Fairwind sync). The registry resolves
tickets via `Project.jira_project_key` + aliases — so we must copy *all* account keys
onto the project (primary key + aliases) whenever designers log time on those boards.
"""

from __future__ import annotations

import re
from collections import defaultdict
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


def fairwind_account_ids_for_jira_keys(
    session: Session, jira_keys: set[str]
) -> set[str]:
    """Fairwind account ids whose Account/Project Jira keys intersect ``jira_keys``."""
    wanted = {k.strip().upper() for k in jira_keys if k and str(k).strip()}
    if not wanted:
        return set()

    found: set[str] = set()
    for acct in session.query(Account).all():
        fw = (acct.fairwind_account_id or "").strip()
        if not fw:
            continue
        acct_keys = {str(k).strip().upper() for k in (acct.jira_project_keys or []) if k}
        if acct_keys & wanted:
            found.add(fw)

    for proj in session.query(Project).filter(Project.jira_project_key.isnot(None)).all():
        pk = (proj.jira_project_key or "").strip().upper()
        fw = (proj.fairwind_account_id or "").strip()
        if pk and fw and pk in wanted:
            found.add(fw)
    # Also match aliases that look like Jira keys (synced from account keys).
    for proj in session.query(Project).all():
        fw = (proj.fairwind_account_id or "").strip()
        if not fw:
            continue
        aliases = {str(a).strip().upper() for a in (proj.aliases or []) if a}
        if aliases & wanted:
            found.add(fw)
    return found


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
    for fw in fairwind_account_ids_for_jira_keys(session, wanted):
        acct = session.query(Account).filter_by(fairwind_account_id=fw).one_or_none()
        if acct:
            to_enable[fw] = acct

    def _matched(acct: Account) -> list[str]:
        have = {str(k).strip().upper() for k in (acct.jira_project_keys or []) if k}
        fw = (acct.fairwind_account_id or "").strip()
        if fw:
            for proj in session.query(Project).filter_by(fairwind_account_id=fw).all():
                pk = (proj.jira_project_key or "").strip().upper()
                if pk:
                    have.add(pk)
                for a in proj.aliases or []:
                    aa = str(a).strip().upper()
                    if aa:
                        have.add(aa)
        return sorted(have & wanted)

    return _enable_accounts(
        session,
        to_enable,
        enabled_by=enabled_by,
        note_fn=lambda matched: (
            f"Auto-enabled — design team had Jira work on "
            f"{', '.join(matched) if matched else 'matching Jira project'}."
        ),
        matched_keys_fn=_matched,
    )


def _enable_accounts(
    session: Session,
    to_enable: dict[str, Account],
    *,
    enabled_by: str | None,
    note_fn,
    matched_keys_fn,
) -> list[Account]:
    newly: list[Account] = []
    now = datetime.now()
    for acct in to_enable.values():
        if acct.digest_enabled:
            continue
        matched = matched_keys_fn(acct)
        acct.digest_enabled = True
        acct.enabled_by = enabled_by
        acct.enabled_at = now
        acct.notes = note_fn(matched)
        session.add(acct)
        ensure_project_for_account(session, acct)
        newly.append(acct)
    if newly:
        session.flush()
    return newly


def enable_accounts_for_mentioned_projects(
    session: Session,
    *,
    fairwind_account_ids: set[str] | None = None,
    project_names: set[str] | None = None,
    enabled_by: str | None = None,
) -> list[Account]:
    """Enable Fairwind accounts for projects designers actually mentioned.

    Used by the daily digest: if a daily names Northerner / Amiel / … and that
    project is linked to an account, turn digest_enabled on so we export it.
    """
    wanted_fw = {str(x).strip() for x in (fairwind_account_ids or []) if x and str(x).strip()}
    wanted_names = {
        str(n).strip().lower() for n in (project_names or []) if n and str(n).strip()
    }
    if not wanted_fw and not wanted_names:
        return []

    to_enable: dict[str, Account] = {}

    if wanted_fw:
        for acct in session.query(Account).all():
            fw = (acct.fairwind_account_id or "").strip()
            if fw and fw in wanted_fw:
                to_enable[fw] = acct

    if wanted_names:
        for proj in session.query(Project).all():
            names = { (proj.canonical_name or "").strip().lower() }
            names |= { (a or "").strip().lower() for a in (proj.aliases or []) if a }
            if not (names & wanted_names):
                continue
            fw = (proj.fairwind_account_id or "").strip()
            if not fw or fw in to_enable:
                continue
            acct = session.query(Account).filter_by(fairwind_account_id=fw).one_or_none()
            if acct:
                to_enable[fw] = acct

    return _enable_accounts(
        session,
        to_enable,
        enabled_by=enabled_by,
        note_fn=lambda _m: (
            "Auto-enabled from daily digest — a designer mentioned this project."
        ),
        matched_keys_fn=lambda _acct: [],
    )


_ALIAS_MIN_LEN = 6  # free-text: avoid Tempo/Puma/Posti/Weekend-style false enables
_ALIAS_TOKEN_RE_CACHE: dict[str, re.Pattern] = {}


def _alias_token_re(alias: str) -> re.Pattern:
    if alias not in _ALIAS_TOKEN_RE_CACHE:
        _ALIAS_TOKEN_RE_CACHE[alias] = re.compile(
            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
            re.IGNORECASE,
        )
    return _ALIAS_TOKEN_RE_CACHE[alias]


def collect_mentioned_fairwind_ids(documents: list, registry) -> set[str]:
    """Fairwind account ids for projects named in *designer dailies*.

    Scans Fairwind internal-folder messages and cro@ mail (roster dailies land in
    both). Free-text alias hits require length ≥ 6 so short/common names
    (Tempo, Puma, Weekend) do not auto-enable dozens of unrelated accounts.
    """
    found: set[str] = set()
    aliases = [
        (alias, entry)
        for alias, entry in registry.aliases_longest_first()
        if len(alias) >= _ALIAS_MIN_LEN and entry.fairwind_account_id
    ]
    for doc in documents or []:
        raw = getattr(doc, "raw", None) or {}
        folder = raw.get("folder")
        is_daily = folder == "internal" or (
            getattr(doc, "source", None) == "gmail" and folder == "cro"
        )
        # Dailies only — not every Jira/transcript in the corpus.
        if not is_daily:
            # Still honour an explicit project_hint on any doc (exact registry resolve).
            hint = getattr(doc, "project_hint", None)
            entry = None
            if getattr(doc, "source", None) == "jira":
                entry = registry.resolve_jira_key(hint) or registry.resolve(hint)
            else:
                entry = registry.resolve(hint)
            if entry and entry.fairwind_account_id:
                found.add(entry.fairwind_account_id)
            continue
        hint = getattr(doc, "project_hint", None)
        entry = registry.resolve(hint)
        if entry and entry.fairwind_account_id:
            found.add(entry.fairwind_account_id)
        text = f"{getattr(doc, 'title', None) or ''} {getattr(doc, 'body', None) or ''}"
        if not text.strip():
            continue
        for alias, entry in aliases:
            if entry.fairwind_account_id in found:
                continue
            if _alias_token_re(alias).search(text):
                found.add(entry.fairwind_account_id)
    return found


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


def _normalize_keys(keys) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for k in keys or []:
        kk = str(k).strip().upper()
        if not kk or kk in seen:
            continue
        seen.add(kk)
        out.append(kk)
    return out


def _apply_account_jira_keys(project: Project, account: Account) -> bool:
    """Copy *all* Account.jira_project_keys onto the project for registry resolution.

    - Sets ``jira_project_key`` when missing (first account key).
    - Adds every account key as an alias so DCP1 / JYDP / AAD resolve even when the
      account has multiple Jira boards (Amiel, JYSK, …).

    Returns True if the project row changed.
    """
    keys = _normalize_keys(account.jira_project_keys)
    if not keys:
        return False
    changed = False
    current = (project.jira_project_key or "").strip().upper()
    if not current:
        project.jira_project_key = keys[0]
        changed = True
    aliases = list(project.aliases or [])
    existing = {a.strip().upper() for a in aliases if a and str(a).strip()}
    for k in keys:
        if k not in existing:
            aliases.append(k)
            existing.add(k)
            changed = True
    if changed:
        project.aliases = aliases
    return changed


def absorb_jira_keys_from_docs(session: Session, docs: list) -> dict:
    """Learn Jira keys from Fairwind/Jira docs onto Account.jira_project_keys.

    When a designer logs time on a board under a Fairwind account export, the doc
    carries ``account_id`` + ``project_hint`` (e.g. DCP1). If that key isn't on the
    account yet (new board on an existing client), append it and link the project.
    """
    by_acct: dict[str, set[str]] = defaultdict(set)
    for doc in docs or []:
        aid = (getattr(doc, "account_id", None) or "").strip()
        if not aid:
            continue
        raw = getattr(doc, "raw", None) or {}
        for candidate in (
            getattr(doc, "project_hint", None),
            raw.get("project_key"),
        ):
            k = (candidate or "").strip().upper()
            if k:
                by_acct[aid].add(k)
        issue = (raw.get("key") or getattr(doc, "external_id", None) or "").strip().upper()
        if "-" in issue and getattr(doc, "source", None) == "jira":
            by_acct[aid].add(issue.rsplit("-", 1)[0])

    learned: list[dict] = []
    for aid, keys in by_acct.items():
        acct = session.query(Account).filter_by(fairwind_account_id=aid).one_or_none()
        if acct is None:
            continue
        existing = set(_normalize_keys(acct.jira_project_keys))
        new = sorted(keys - existing)
        if new:
            acct.jira_project_keys = _normalize_keys(list(acct.jira_project_keys or []) + new)
            session.add(acct)
            learned.append({"account": acct.name, "keys": new})
        ensure_project_for_account(session, acct)
    if by_acct:
        session.flush()
    return {"accounts_learned": len(learned), "learned": learned}


def ensure_projects_for_jira_keys(
    session: Session,
    jira_keys: set[str],
    *,
    fairwind_jira_projects: list[dict] | None = None,
    jira_get_project=None,
) -> dict:
    """Ensure every roster Jira key resolves in the project registry.

    Clears ``unmatched_project`` flags for keys that exist in Jira / Fairwind but
    were never copied onto ``Account`` / ``Project``:

    1. Already on an Account or Project → sync aliases.
    2. Fairwind ``/jira-projects`` maps key → account → append key + ensure project.
    3. Else Jira Cloud project name → link by account name, or create a standalone
       Project row (internal boards like SID / GROW with no Fairwind client).
    """
    wanted = {k.strip().upper() for k in jira_keys if k and str(k).strip()}
    if not wanted:
        return {"ensured": 0, "linked": [], "created": [], "already": []}

    fw_by_key: dict[str, dict] = {}
    for row in fairwind_jira_projects or []:
        key = str(row.get("key") or "").strip().upper()
        if not key:
            continue
        acct = row.get("account") if isinstance(row.get("account"), dict) else {}
        fw = (
            (acct.get("id") if acct else None)
            or row.get("account_id")
            or ""
        )
        fw = str(fw).strip()
        name = (row.get("name") or "").strip()
        acct_name = ((acct.get("name") if acct else None) or row.get("account_name") or "").strip()
        fw_by_key[key] = {
            "key": key,
            "name": name,
            "fairwind_account_id": fw,
            "account_name": acct_name,
        }

    already: list[str] = []
    linked: list[dict] = []
    created: list[dict] = []

    def _known(key: str) -> bool:
        return bool(fairwind_account_ids_for_jira_keys(session, {key})) or any(
            (p.jira_project_key or "").strip().upper() == key
            or key in {str(a).strip().upper() for a in (p.aliases or []) if a}
            for p in session.query(Project).all()
        )

    def _attach_key_to_account(acct: Account, key: str, jira_name: str | None) -> Project:
        keys = _normalize_keys(list(acct.jira_project_keys or []) + [key])
        if keys != _normalize_keys(acct.jira_project_keys):
            acct.jira_project_keys = keys
            session.add(acct)
        proj = ensure_project_for_account(session, acct)
        # Prefer a human Jira board name as an alias when useful.
        if jira_name:
            aliases = list(proj.aliases or [])
            existing = {a.strip().lower() for a in aliases if a}
            if jira_name.strip().lower() not in existing:
                aliases.append(jira_name.strip())
                proj.aliases = aliases
                session.add(proj)
        return proj

    def _account_by_name(jira_name: str) -> Account | None:
        base = (jira_name or "").split("|")[0].strip().lower()
        if len(base) < 4:
            return None
        best: Account | None = None
        for acct in session.query(Account).all():
            an = (acct.name or "").strip().lower()
            if not an:
                continue
            if an == base or base.startswith(an) or an.startswith(base):
                if best is None or len(an) > len((best.name or "")):
                    best = acct
        return best

    for key in sorted(wanted):
        if _known(key):
            # Still push Account keys → Project aliases when the account already has it.
            for fw in fairwind_account_ids_for_jira_keys(session, {key}):
                acct = session.query(Account).filter_by(fairwind_account_id=fw).one_or_none()
                if acct:
                    ensure_project_for_account(session, acct)
            already.append(key)
            continue

        fw_hit = fw_by_key.get(key)
        jira_meta = None
        if jira_get_project is not None:
            try:
                jira_meta = jira_get_project(key)
            except Exception:  # noqa: BLE001 — fall through to Fairwind name
                jira_meta = None
        jira_name = (
            (jira_meta or {}).get("name")
            or (fw_hit or {}).get("name")
            or key
        )

        acct: Account | None = None
        fw = (fw_hit or {}).get("fairwind_account_id") or ""
        if fw:
            acct = session.query(Account).filter_by(fairwind_account_id=fw).one_or_none()
        if acct is None:
            acct = _account_by_name(jira_name)

        if acct is not None:
            proj = _attach_key_to_account(acct, key, jira_name)
            linked.append(
                {
                    "key": key,
                    "account": acct.name,
                    "project": proj.canonical_name,
                    "fairwind_account_id": acct.fairwind_account_id,
                }
            )
            continue

        # Standalone project (internal / unmapped boards).
        existing = next(
            (
                p
                for p in session.query(Project).all()
                if (p.jira_project_key or "").strip().upper() == key
            ),
            None,
        )
        if existing is None:
            # Name collision without key — attach the key.
            existing = next(
                (
                    p
                    for p in session.query(Project).all()
                    if (p.canonical_name or "").strip() == jira_name
                ),
                None,
            )
        if existing is not None:
            if not (existing.jira_project_key or "").strip():
                existing.jira_project_key = key
            aliases = list(existing.aliases or [])
            have = {a.strip().upper() for a in aliases if a}
            for a in (key, jira_name):
                if a and a.strip().upper() not in have:
                    aliases.append(a.strip())
                    have.add(a.strip().upper())
            existing.aliases = aliases
            session.add(existing)
            linked.append(
                {
                    "key": key,
                    "account": None,
                    "project": existing.canonical_name,
                    "fairwind_account_id": existing.fairwind_account_id,
                }
            )
            continue

        proj = Project(
            canonical_name=jira_name,
            aliases=[key, jira_name],
            jira_project_key=key,
            fairwind_account_id=None,
            active=True,
            track_daily=True,
            notes=f"Auto-created from roster Jira key {key} (no Fairwind account).",
        )
        session.add(proj)
        created.append({"key": key, "project": jira_name})

    if linked or created:
        session.flush()
    return {
        "ensured": len(linked) + len(created),
        "linked": linked,
        "created": created,
        "already": already,
    }


def sync_jira_keys_to_projects(session: Session, docs: list | None = None) -> dict:
    """Ensure every Account's Jira keys resolve via its Project row.

    1. Optionally absorb new keys seen on corpus docs (new boards).
    2. Push all account keys onto linked projects (primary + aliases).

    Call on every daily/weekly run *before* ProjectRegistry / filter so
    unmatched_project flags don't fire for keys Fairwind already knows.
    """
    learned = absorb_jira_keys_from_docs(session, docs or [])
    synced: list[dict] = []
    for acct in session.query(Account).all():
        if not acct.fairwind_account_id:
            continue
        if not _normalize_keys(acct.jira_project_keys):
            continue
        before_key = None
        before_aliases: set[str] = set()
        existing = (
            session.query(Project)
            .filter_by(fairwind_account_id=acct.fairwind_account_id)
            .first()
        )
        if existing:
            before_key = (existing.jira_project_key or "").strip().upper() or None
            before_aliases = {a.strip().upper() for a in (existing.aliases or []) if a}
        proj = ensure_project_for_account(session, acct)
        after_key = (proj.jira_project_key or "").strip().upper() or None
        after_aliases = {a.strip().upper() for a in (proj.aliases or []) if a}
        if after_key != before_key or after_aliases - before_aliases:
            synced.append(
                {
                    "account": acct.name,
                    "project": proj.canonical_name,
                    "jira_project_key": after_key,
                    "keys_added": sorted(after_aliases - before_aliases),
                }
            )
    if synced:
        session.flush()
    return {
        "accounts_learned": learned.get("accounts_learned", 0),
        "learned": learned.get("learned", []),
        "projects_synced": len(synced),
        "synced": synced,
    }


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

    Account-linked keys are hints only — a key that is merely a prefix of the
    Fairwind name (HOITO ⊂ Hoitolatukku) must not beat a clearer Jira match like
    HOITOR ("Hoitolatukku | Redesign"). Service Cloud boards are deprioritized vs
    redesign / design-named projects.
    """
    name = (project_name or "").strip()
    nl = name.lower()
    fid = (fairwind_account_id or "").strip()
    by_key: dict[str, dict] = {}

    def _name_bias(pname: str | None) -> int:
        pl = (pname or "").strip().lower()
        if not pl:
            return 0
        if any(
            tok in pl
            for tok in ("redesign", "| design", " ux", "ui/ux", "design system")
        ) or ("design" in pl and "service" not in pl and "support" not in pl):
            return -1
        if "service cloud" in pl:
            return 2
        return 0

    def _add(key: str, pname: str | None, source: str, score: int) -> None:
        k = (key or "").strip().upper()
        if not k:
            return
        display = (pname or "").strip() or k
        score = max(0, int(score) + _name_bias(display))
        row = by_key.get(k)
        if row is None or score < row["score"]:
            by_key[k] = {
                "key": k,
                "name": display,
                "source": source,
                "score": score,
            }
            return
        if display not in (name, k) and row["name"] in (name, k, ""):
            row["name"] = display

    titles: dict[str, str] = {}
    for r in list(fairwind_jira_projects or []) + list(jira_cloud_projects or []):
        key = str(r.get("key") or "").strip().upper()
        pname = (r.get("name") or "").strip()
        if key and pname:
            titles[key] = pname

    for k in account_keys or []:
        kk = str(k).strip().upper()
        if not kk:
            continue
        # Hint only — never treat key⊂account-name as exact (HOITO / Hoitolatukku).
        _add(kk, titles.get(kk) or name, "fairwind_account", 2)

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
