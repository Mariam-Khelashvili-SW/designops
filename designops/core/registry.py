"""Project canonicalisation (§3, §11.4).

The registry is NOT a filter — it maps the many strings people type ("Redecker",
"Nicokick / Northerner", "SGD") to one canonical project. Resolution is EXACT on a
normalized alias, never fuzzy: a miss routes to the `unmatched_project` flag rather
than silently over-matching (§11.4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


def normalize(s: str) -> str:
    """Lowercase + collapse internal whitespace. Deliberately conservative — we want
    'Redecker' and 'Redeker Aesthetics' to be *distinct* aliases both listed on the
    project, not fuzzily merged."""
    return " ".join(s.split()).lower()


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    id: uuid.UUID
    canonical_name: str
    jira_project_key: str | None
    fairwind_account_id: str | None


class ProjectRegistry:
    def __init__(self, entries: list[tuple[ProjectEntry, list[str]]]):
        self._entries = [e for e, _ in entries]
        self._by_alias: dict[str, ProjectEntry] = {}
        self._by_jira_key: dict[str, ProjectEntry] = {}
        self._by_account: dict[str, ProjectEntry] = {}
        for entry, aliases in entries:
            for a in [entry.canonical_name, *aliases]:
                key = normalize(a)
                existing = self._by_alias.get(key)
                # Prefer the mapping that has a Fairwind account (Enmedify seed vs MediCrops).
                if existing is None or (
                    not existing.fairwind_account_id and entry.fairwind_account_id
                ):
                    self._by_alias[key] = entry
                raw = (a or "").strip()
                if raw and " " not in raw and raw.replace("_", "").isalnum():
                    jk = raw.upper()
                    prev = self._by_jira_key.get(jk)
                    if prev is None or (
                        not prev.fairwind_account_id and entry.fairwind_account_id
                    ):
                        self._by_jira_key[jk] = entry
            if entry.jira_project_key:
                jk = entry.jira_project_key.upper()
                prev = self._by_jira_key.get(jk)
                if prev is None or (
                    not prev.fairwind_account_id and entry.fairwind_account_id
                ):
                    self._by_jira_key[jk] = entry
            if entry.fairwind_account_id:
                self._by_account[entry.fairwind_account_id] = entry

    @classmethod
    def from_rows(cls, rows, accounts=None) -> ProjectRegistry:
        """Build registry from Project rows.

        Optional ``accounts``: also index every ``Account.jira_project_keys`` entry to
        the project linked by ``fairwind_account_id``. Fairwind often lists many boards
        per client (Amiel → AAD, AMIEL, …); only syncing ``Project.jira_project_key``
        would miss the rest.

        Rows with a Fairwind account are indexed first so a linked MediCrops wins
        over an orphan Enmedify seed that shares aliases.
        """
        ordered = sorted(
            rows,
            key=lambda r: (
                0 if getattr(r, "fairwind_account_id", None) else 1,
                (getattr(r, "canonical_name", None) or ""),
            ),
        )
        reg = cls(
            [
                (
                    ProjectEntry(
                        id=r.id,
                        canonical_name=r.canonical_name,
                        jira_project_key=r.jira_project_key,
                        fairwind_account_id=r.fairwind_account_id,
                    ),
                    list(r.aliases or []),
                )
                for r in ordered
            ]
        )
        if not accounts:
            return reg
        by_fw = {
            e.fairwind_account_id: e
            for e in reg._entries
            if e.fairwind_account_id
        }
        for acct in accounts:
            fw = (getattr(acct, "fairwind_account_id", None) or "").strip()
            entry = by_fw.get(fw)
            if entry is None:
                continue
            for k in getattr(acct, "jira_project_keys", None) or []:
                key = str(k).strip().upper()
                if not key:
                    continue
                reg._by_jira_key[key] = entry
                reg._by_alias[normalize(key)] = entry
        return reg

    def resolve(self, text: str | None) -> ProjectEntry | None:
        """Exact (normalized) alias match. None = unmatched → Unassigned + flag."""
        if not text:
            return None
        return self._by_alias.get(normalize(text))

    def resolve_jira_key(self, key: str | None) -> ProjectEntry | None:
        if not key:
            return None
        return self._by_jira_key.get(key.upper())

    def resolve_account(self, account_id: str | None) -> ProjectEntry | None:
        """Map a Fairwind account id to its project (used to place beyond-daily signal)."""
        if not account_id:
            return None
        return self._by_account.get(account_id)

    def aliases_longest_first(self) -> list[tuple[str, ProjectEntry]]:
        """(normalized alias, entry) pairs, longest alias first — for scanning free text."""
        return sorted(self._by_alias.items(), key=lambda kv: (-len(kv[0]), kv[0]))
