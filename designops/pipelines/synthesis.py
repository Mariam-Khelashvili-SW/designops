"""Synthesis stage (§6.3) — two-pass LLM (signals then digest).

Pass A emits required R1–R6 `signals` JSON. Pass B emits the Growth-Pulse digest and may
only verbalize findings present in the locked signals. Scope is already enforced
upstream; this stage carries judgement (§2 fix #10).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from designops.adapters.llm import LLMClient, LLMResult, parse_digest_json
from designops.pipelines.daily_signals import (
    attach_agent_notes,
    empty_signals,
    enforce_intelligence_artifacts,
    fix_leave_duration_labels,
    materialize_intelligence,
    validate_signals,
)
from designops.pipelines.filter import FilterResult

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "daily-ops-digest.md"

_PASS_A_SUFFIX = """

---

## THIS CALL — Pass A only

Emit **only** a JSON object with a top-level `signals` key covering all six rule slots
(R1–R6). Do **not** emit `status`, `needs_review`, `at_a_glance`, or any email prose.
Every key is required. Empty findings must include a `checked` reason. Every finding
needs `evidence` with verbatim `quote` + `source`.
"""

_PASS_B_SUFFIX = """

---

## THIS CALL — Pass B only

Emit the full Growth-Pulse digest JSON (status, needs_review, open_questions,
todays_plans, no_report, at_a_glance, escalations, heads_ups).

The locked Pass A `signals` JSON is provided in the user message. You may **only**
verbalize intelligence findings that appear there — copy them into `escalations`,
`heads_ups`, and `status[].agent_note` as appropriate. Do not invent new escalations /
heads-ups / agent notes absent from signals. Echo the locked `signals` object in your
output.

When phrasing heads-ups and agent notes, keep them short and human (one sentence when
possible) — same voice as a sticky note to Olga, not a log line.
"""


def build_system_prompt(
    report_date: date,
    roster_names: list[dict],
    project_names: list[dict],
    tracked_accounts: list[str] | None = None,
    *,
    pass_label: str = "B",
) -> str:
    skill = _SKILL.read_text(encoding="utf-8")
    roster_block = "\n".join(_roster_line(r) for r in roster_names)
    registry_block = "\n".join(
        f"- {p['canonical_name']}"
        + (f"  [aliases: {', '.join(p['aliases'])}]" if p.get("aliases") else "")
        for p in project_names
    )
    tracked_block = "\n".join(f"- {name}" for name in (tracked_accounts or [])) or "(none)"
    base = (
        skill
        .replace("{roster}", roster_block)
        .replace("{project_registry}", registry_block)
        .replace("{tracked_accounts}", tracked_block)
        .replace("{report_date}", report_date.isoformat())
    )
    return base + (_PASS_A_SUFFIX if pass_label == "A" else _PASS_B_SUFFIX)


def _roster_line(r: dict) -> str:
    name = r.get("full_name") or ""
    status = r.get("status", "active")
    lf = r.get("leave_from")
    lu = r.get("leave_until")
    if lf or lu:
        return f"- {name} ({status}; leave {lf or '?'} → {lu or '?'})"
    return f"- {name} ({status})"


def build_user_content(
    filtered: FilterResult,
    *,
    leave_calendar: list[dict] | None = None,
    prior_next: list[dict] | None = None,
) -> str:
    """Filtered corpus + leave calendar + prior Next run-log."""
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

    chunks.append("## Leave calendar (approved leave windows)")
    if leave_calendar:
        for row in leave_calendar:
            duration = row.get("duration_label") or ""
            dur_part = f", {duration}" if duration else ""
            chunks.append(
                f"- {row.get('full_name')}: "
                f"{row.get('leave_from') or '?'} → {row.get('leave_until') or '?'} "
                f"({row.get('status', 'on_leave')}{dur_part})"
            )
    else:
        chunks.append("- (none on roster for this window)")
    chunks.append("")

    chunks.append("## Prior Next run-log (recent digests — for R4 / R6)")
    if prior_next:
        for run in prior_next:
            chunks.append(f"### Report date {run.get('report_date')}")
            for row in run.get("next_items") or []:
                person = row.get("person") or ""
                proj = row.get("project") or ""
                nxt = row.get("next") or ""
                if nxt:
                    chunks.append(f"- {person} · {proj}: {nxt}")
            chunks.append("")
    else:
        chunks.append("- (no prior digest JSON available yet)")
        chunks.append("")

    return "\n".join(chunks).strip()


def synthesize(
    filtered: FilterResult,
    report_date: date,
    roster_names: list[dict],
    project_names: list[dict],
    *,
    tracked_accounts: list[str] | None = None,
    leave_calendar: list[dict] | None = None,
    prior_next: list[dict] | None = None,
    client: LLMClient | None = None,
) -> tuple[dict, LLMResult]:
    """Two-pass synthesize → (digest_json, combined LLMResult)."""
    client = client or LLMClient()
    roster_set = {r["full_name"] for r in roster_names if r.get("full_name")}
    user = build_user_content(
        filtered, leave_calendar=leave_calendar, prior_next=prior_next
    )

    # --- Pass A: signals ---
    system_a = build_system_prompt(
        report_date, roster_names, project_names, tracked_accounts, pass_label="A"
    )
    result_a = client.synthesize(system=system_a, user_content=user, max_tokens=6000)
    raw_a = parse_digest_json(result_a.text)
    signals_raw = raw_a.get("signals") if isinstance(raw_a.get("signals"), dict) else raw_a
    signals = validate_signals(signals_raw, roster_names=roster_set)

    # --- Pass B: full digest ---
    system_b = build_system_prompt(
        report_date, roster_names, project_names, tracked_accounts, pass_label="B"
    )
    user_b = (
        user
        + "\n\n## Locked Pass A signals (verbalize only these intelligence findings)\n```json\n"
        + json.dumps({"signals": signals}, ensure_ascii=False, indent=2)
        + "\n```"
    )
    result_b = client.synthesize(system=system_b, user_content=user_b, max_tokens=8000)
    digest = parse_digest_json(result_b.text)
    digest["signals"] = signals

    escalations, heads_ups, agent_notes = materialize_intelligence(signals)
    # Signals are authoritative — Pass B may only verbalize these findings.
    digest["escalations"] = [
        {k: v for k, v in e.items() if not k.startswith("_")} for e in escalations
    ]
    digest["heads_ups"] = [
        {k: v for k, v in h.items() if not k.startswith("_")} for h in heads_ups
    ]
    enforce_intelligence_artifacts(digest)
    fix_leave_duration_labels(digest, leave_calendar)
    attach_agent_notes(digest.setdefault("status", []), agent_notes)

    _validate_shape(digest)
    _prune_ungrounded(digest, user, project_names)

    combined = LLMResult(
        text=result_b.text,
        input_tokens=result_a.input_tokens + result_b.input_tokens,
        output_tokens=result_a.output_tokens + result_b.output_tokens,
        cost_usd=round(float(result_a.cost_usd) + float(result_b.cost_usd), 4),
        model=result_b.model or result_a.model,
    )
    return digest, combined


def _prune_ungrounded(digest: dict, user_content: str, project_names: list[dict]) -> None:
    """Drop rows whose project name (or a registry alias) never appears in the corpus.
    Then recompute glance KPIs from what survives. Rows with no project are kept."""
    import re

    uc = user_content.lower()
    alias_map = {
        p.get("canonical_name", "").lower(): [p.get("canonical_name", ""), *p.get("aliases", [])]
        for p in project_names
    }
    # Flat list of known labels for substring / segment matching on intelligence rows.
    known_labels = []
    for p in project_names:
        known_labels.append(p.get("canonical_name") or "")
        known_labels.extend(p.get("aliases") or [])
    known_labels = [x for x in known_labels if x and str(x).strip()]

    def grounded(name: str | None) -> bool:
        if not name or not str(name).strip():
            return True
        candidates = alias_map.get(name.strip().lower(), [name])
        return any(a and a.lower() in uc for a in candidates)

    def grounded_intelligence(name: str | None) -> bool:
        """Intelligence findings may cite one registry project, or (mistakenly) a
        compound like 'Acer / Club Portal'. Keep if any segment or known label
        appears in the corpus — do not drop R1 leave escalations for slash names.
        """
        if not name or not str(name).strip():
            return True
        if grounded(name):
            return True
        raw = str(name).strip()
        # Segment on / | ; and commas — common LLM compound separators.
        parts = [
            p.strip()
            for p in re.split(r"[/;|,]|\\band\\b", raw, flags=re.IGNORECASE)
            if p and p.strip()
        ]
        for part in parts:
            # Strip parenthetical aliases: "Club Portal (SGDCP)" → "Club Portal"
            base = re.sub(r"\([^)]*\)", "", part).strip()
            if base and grounded(base):
                return True
            if part and grounded(part):
                return True
        # Any known registry label that is a substring of the compound and in corpus.
        low = raw.lower()
        for label in known_labels:
            if label.lower() in low and label.lower() in uc:
                return True
        return False

    digest["status"] = [
        s for s in digest.get("status", []) if grounded(s.get("project"))
    ]
    digest["needs_review"] = [
        r for r in digest.get("needs_review", []) if grounded(r.get("project"))
    ]
    digest["open_questions"] = [
        q for q in digest.get("open_questions", []) if grounded(q.get("project"))
    ]
    digest["escalations"] = [
        e for e in (digest.get("escalations") or []) if grounded_intelligence(e.get("project"))
    ]
    digest["heads_ups"] = [
        h for h in (digest.get("heads_ups") or []) if grounded_intelligence(h.get("project"))
    ]

    g = digest.setdefault("at_a_glance", {})
    review = digest.get("needs_review") or []
    escalations = [
        e
        for e in (digest.get("escalations") or [])
        if not str(e.get("text") or "").startswith("+")
    ]
    g["need_review"] = len(review) + len(escalations)
    g["blocked"] = sum(1 for r in review if r.get("blocked"))
    g["active"] = len({s.get("person") for s in digest.get("status", []) if s.get("person")})


def _validate_shape(digest: dict) -> None:
    for key in (
        "at_a_glance",
        "status",
        "needs_review",
        "open_questions",
        "todays_plans",
        "no_report",
    ):
        if key not in digest:
            raise ValueError(f"digest JSON missing '{key}': {json.dumps(digest)[:200]}")
    digest.setdefault("escalations", [])
    digest.setdefault("heads_ups", [])
    digest.setdefault("signals", empty_signals())
