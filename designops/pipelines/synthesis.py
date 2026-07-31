"""Synthesis stage (§6.3) — the ONE LLM call.

Assembles the system prompt (skill file + roster + project registry + report_date) and
the user content (filtered corpus, verbatim, grouped by person), asks for structured
JSON, and parses it. Scope is already enforced upstream, so this stage only carries
judgement (§2 fix #10).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from designops.adapters.llm import LLMClient, LLMResult, parse_digest_json
from designops.pipelines.filter import FilterResult

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "daily-ops-digest.md"


def build_system_prompt(
    report_date: date,
    roster_names: list[dict],
    project_names: list[dict],
    tracked_accounts: list[str] | None = None,
) -> str:
    skill = _SKILL.read_text(encoding="utf-8")
    roster_block = "\n".join(
        f"- {r['full_name']} ({r.get('status', 'active')})" for r in roster_names
    )
    registry_block = "\n".join(
        f"- {p['canonical_name']}"
        + (f"  [aliases: {', '.join(p['aliases'])}]" if p.get("aliases") else "")
        for p in project_names
    )
    tracked_block = "\n".join(f"- {name}" for name in (tracked_accounts or [])) or "(none)"
    return (
        skill
        .replace("{roster}", roster_block)
        .replace("{project_registry}", registry_block)
        .replace("{tracked_accounts}", tracked_block)
        .replace("{report_date}", report_date.isoformat())
    )


def build_user_content(filtered: FilterResult) -> str:
    """Filtered corpus, verbatim, grouped by person (§6.3)."""
    by_person: dict[str, list] = {}
    for inc in filtered.included:
        by_person.setdefault(inc.person.full_name, []).append(inc)

    chunks: list[str] = ["## Daily reports (grouped by person)"]
    for name, docs in by_person.items():
        chunks.append(f"### {name}")
        for inc in docs:
            proj = inc.project.canonical_name if inc.project else "Unassigned"
            hint = inc.document.project_hint or proj
            chunks.append(
                f"- [{inc.document.source} · project: {hint}] "
                f"{inc.document.title}\n  {inc.document.body}"
            )
        chunks.append("")

    # Beyond-the-dailies: report-day client email / transcript signal, grouped by project.
    # Mine sparingly per the skill — only blockers/escalations/heads-ups no daily carried.
    if filtered.beyond_daily:
        by_project: dict[str, list] = {}
        for bd in filtered.beyond_daily:
            by_project.setdefault(bd.label, []).append(bd.document)
        chunks.append("## Beyond the dailies (report-day client email / transcripts / cro@)")
        for label, bdocs in by_project.items():
            chunks.append(f"### {label}")
            for d in bdocs:
                src = d.raw.get("mailbox") or d.source
                if d.raw.get("folder") == "cro":
                    src = f"cro@ ({d.raw.get('from') or d.author_identity})"
                chunks.append(f"- [{src}] {d.title}\n  {d.body}")
            chunks.append("")
    return "\n".join(chunks).strip()


def synthesize(
    filtered: FilterResult,
    report_date: date,
    roster_names: list[dict],
    project_names: list[dict],
    *,
    tracked_accounts: list[str] | None = None,
    client: LLMClient | None = None,
) -> tuple[dict, LLMResult]:
    system = build_system_prompt(report_date, roster_names, project_names, tracked_accounts)
    user = build_user_content(filtered)
    client = client or LLMClient()
    result = client.synthesize(system=system, user_content=user)
    digest = parse_digest_json(result.text)
    _validate_shape(digest)
    _prune_ungrounded(digest, user, project_names)
    return digest, result


def _prune_ungrounded(digest: dict, user_content: str, project_names: list[dict]) -> None:
    """Drop anything the model invented from the registry rather than the corpus: a project
    GROUP or an `unmatched_projects` entry whose name (or a registry alias of it) doesn't
    actually appear in the dailies this run. Then recompute the blocked/escalation KPIs
    over what survives."""
    uc = user_content.lower()
    alias_map = {
        p.get("canonical_name", "").lower(): [p.get("canonical_name", ""), *p.get("aliases", [])]
        for p in project_names
    }

    def grounded(name: str) -> bool:
        candidates = alias_map.get(name.strip().lower(), [name])
        return any(a and a.lower() in uc for a in candidates)

    digest["projects"] = [p for p in digest.get("projects", []) if grounded(str(p.get("name", "")))]
    ums = digest.get("unmatched_projects")
    if isinstance(ums, list):
        digest["unmatched_projects"] = [u for u in ums if grounded(str(u.get("project", "")))]

    g = digest.setdefault("at_a_glance", {})
    people = [pp for p in digest["projects"] for pp in p.get("people", [])]
    g["blocked"] = sum(1 for pp in people if pp.get("blocker"))
    g["escalations"] = sum(1 for pp in people if pp.get("escalation"))


def _validate_shape(digest: dict) -> None:
    for key in ("at_a_glance", "projects", "no_report", "action_needed"):
        if key not in digest:
            raise ValueError(f"digest JSON missing '{key}': {json.dumps(digest)[:200]}")
