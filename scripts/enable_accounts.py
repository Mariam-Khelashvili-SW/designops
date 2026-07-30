"""Toggle `account.digest_enabled` — the daily-export allowlist (§11).

An enabled account gets one morning export pulled for it; inside that export the model
decides what is design-team work. This is the code-side scope switch; content relevance
stays with the model.

Usage:
  python -m scripts.enable_accounts                # enable the default design shortlist
  python -m scripts.enable_accounts <id|name> ...  # enable specific accounts
  python -m scripts.enable_accounts --disable <id|name> ...
  python -m scripts.enable_accounts --list         # show what's currently enabled

Matching is by fairwind_account_id (exact) or account name (case-insensitive exact).
`enabled_by` / `enabled_at` are stamped so the Accounts screen shows who turned it on.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from designops.core.config import get_settings
from designops.core.db import session_scope
from designops.core.models import Account
from designops.core.projects import ensure_project_for_account

# §11.5 starting allowlist: the 5 confirmed-by-daily accounts + Wienerberger + the
# needs_resolution set (Furniture Trader & Reuzel carry live blockers). ~14 accounts,
# comfortably inside the morning export window.
DEFAULT_SHORTLIST = [
    "Northerner",
    "Felco",
    "Redeker Aesthetics",
    "The Furniture Trader",
    "Reuzel",
    "University of Michigan",
    "Wienerberger",
    "Sports Group Denmark",
    "Jollyes",
    "Legeakademiet",
    "Enviropack",
    "Everything Wine",
    "Moon Climbing",
    "MediCrops",
]


def _resolve(session, token: str) -> Account | None:
    row = session.query(Account).filter_by(fairwind_account_id=token).one_or_none()
    if row:
        return row
    return (
        session.query(Account)
        .filter(Account.name.ilike(token))
        .one_or_none()
    )


def set_enabled(tokens: list[str], enabled: bool, by: str) -> dict:
    changed, missing = [], []
    with session_scope() as s:
        for tok in tokens:
            row = _resolve(s, tok)
            if row is None:
                missing.append(tok)
                continue
            row.digest_enabled = enabled
            row.enabled_by = by if enabled else None
            row.enabled_at = datetime.now(UTC) if enabled else None
            if enabled:
                ensure_project_for_account(s, row)
            changed.append(row.name)
        s.flush()
        total_enabled = s.query(Account).filter_by(digest_enabled=True).count()
    return {"changed": changed, "missing": missing, "total_enabled": total_enabled}


def list_enabled() -> list[str]:
    with session_scope() as s:
        return [
            f"{a.name}  ({a.fairwind_account_id})"
            for a in s.query(Account).filter_by(digest_enabled=True).order_by(Account.name).all()
        ]


def main(argv: list[str]) -> int:
    by = get_settings().setup_owner_email
    args = argv[1:]
    if args and args[0] == "--list":
        rows = list_enabled()
        print(f"{len(rows)} account(s) enabled for daily export:")
        for r in rows:
            print("  •", r)
        return 0
    disable = False
    if args and args[0] == "--disable":
        disable = True
        args = args[1:]
    tokens = args or (DEFAULT_SHORTLIST if not disable else [])
    if not tokens:
        print("Nothing to do. Pass account ids/names, or --list.")
        return 1
    result = set_enabled(tokens, enabled=not disable, by=by)
    verb = "Disabled" if disable else "Enabled"
    print(f"{verb} {len(result['changed'])}: {', '.join(result['changed']) or '—'}")
    if result["missing"]:
        print(f"⚠ not found (skipped): {', '.join(result['missing'])}")
    print(f"Total enabled now: {result['total_enabled']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
