"""Daily Pulse intelligence layer — signal schema validation (R1–R6).

Pass A must emit all six rule keys with findings or a `checked` reason. Findings without
verbatim evidence are dropped. Blocklisted interpretation words outside quotes fail
validation. Escalations are capped at 5.
"""

from __future__ import annotations

import re
from typing import Any

SIGNAL_KEYS = (
    "R1_leave_x_assignment",
    "R2_client_wait_x_leave",
    "R3_launch_proximity",
    "R4_repeated_next",
    "R5_report_language",
    "R6_rework",
)

MAX_ESCALATIONS = 5

_BLOCKLIST = (
    "behind",
    "less advanced than",
    "struggling",
    "slow",
    "stalled",
    "at risk",
)

_QUOTE_RE = re.compile(r'"[^"]*"|\'[^\']*\'|"[^"]*"')


def empty_signals(*, checked: str = "no findings (fixture / quiet)") -> dict:
    return {k: {"findings": [], "checked": checked} for k in SIGNAL_KEYS}


def _text_outside_quotes(text: str) -> str:
    return _QUOTE_RE.sub(" ", text or "")


def blocklist_hits(text: str) -> list[str]:
    """Return blocklisted phrases found outside quotes (case-insensitive)."""
    hay = _text_outside_quotes(text).lower()
    return [w for w in _BLOCKLIST if w in hay]


def _normalize_evidence(raw: Any) -> list[dict]:
    """Accept list of {quote, source} or a single evidence string → list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [{"quote": s, "source": "source"}] if s else []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw:
        if isinstance(e, str) and e.strip():
            out.append({"quote": e.strip(), "source": "source"})
            continue
        if not isinstance(e, dict):
            continue
        quote = (e.get("quote") or e.get("snippet") or "").strip()
        source = (e.get("source") or "").strip()
        if quote and source:
            out.append({"quote": quote, "source": source})
        elif quote:
            out.append({"quote": quote, "source": "source"})
    return out


def _fmt_evidence_line(evidence: list[dict]) -> str:
    if not evidence:
        return ""
    parts = []
    for e in evidence:
        src = e.get("source") or ""
        if src.startswith("—") or src.startswith("-"):
            parts.append(src)
        else:
            parts.append(f"— {src}" if src else "")
    # Prefer a compact single source line for the email.
    uniq = []
    for p in parts:
        p = p.strip()
        if p and p not in uniq:
            uniq.append(p)
    return " ".join(uniq) if uniq else f"— {evidence[0].get('source') or 'source'}"


def validate_signals(
    signals: dict | None,
    *,
    roster_names: set[str] | None = None,
) -> dict:
    """Validate / normalize Pass A signals. Raises ValueError on schema failure."""
    if not isinstance(signals, dict):
        raise ValueError("signals must be an object")
    out: dict[str, dict] = {}
    for key in SIGNAL_KEYS:
        if key not in signals:
            raise ValueError(f"signals missing required key '{key}'")
        slot = signals[key]
        if not isinstance(slot, dict):
            raise ValueError(f"signals['{key}'] must be an object")
        findings_raw = slot.get("findings")
        if findings_raw is None:
            findings_raw = []
        if not isinstance(findings_raw, list):
            raise ValueError(f"signals['{key}'].findings must be a list")
        kept: list[dict] = []
        for f in findings_raw:
            if not isinstance(f, dict):
                continue
            text = (f.get("text") or "").strip()
            if not text:
                continue
            hits = blocklist_hits(text)
            agent = (f.get("agent_note") or "").strip()
            if agent:
                hits.extend(blocklist_hits(agent))
            if hits:
                raise ValueError(
                    f"signals['{key}'] uses blocklisted word(s) {sorted(set(hits))} "
                    f"outside quotes: {text[:80]!r}"
                )
            evidence = _normalize_evidence(f.get("evidence"))
            if not evidence:
                continue  # no quote → finding does not survive
            who = (f.get("who") or "").strip() or None
            if who and roster_names is not None and who not in roster_names:
                continue
            kind = (f.get("kind") or "").strip().lower() or _infer_kind(key)
            row = {
                "kind": kind,
                "text": text,
                "evidence": evidence,
                "who": who,
                "project": (f.get("project") or "").strip() or None,
                "agent_note": agent or None,
                "why_ranked_here": (f.get("why_ranked_here") or "").strip() or None,
            }
            kept.append(row)
        checked = (slot.get("checked") or "").strip()
        if not kept and not checked:
            raise ValueError(
                f"signals['{key}'] has empty findings but no 'checked' reason"
            )
        out[key] = {"findings": kept}
        if checked:
            out[key]["checked"] = checked
        elif not kept:
            out[key]["checked"] = "no findings"
    return out


def _infer_kind(rule_key: str) -> str:
    if rule_key in ("R1_leave_x_assignment", "R5_report_language"):
        return "escalation"
    if rule_key in ("R2_client_wait_x_leave", "R3_launch_proximity"):
        return "heads_up"
    return "agent_note"


def _overlap_key(text: str, project: str | None = None, who: str | None = None) -> str:
    """Cheap fingerprint for de-duplicating escalation vs heads-up."""
    t = " ".join((text or "").lower().split())
    return f"{(project or '').lower()}|{(who or '').lower()}|{t[:80]}"


def _texts_overlap(a: str, b: str) -> bool:
    """True when one text largely restates the other (shared substantial token span)."""
    ta = {w for w in (a or "").lower().split() if len(w) > 3}
    tb = {w for w in (b or "").lower().split() if len(w) > 3}
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter >= 4 and inter / min(len(ta), len(tb)) >= 0.5


def materialize_intelligence(signals: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Turn validated signals into escalations, heads_ups, and agent_note rows.

    Returns (escalations, heads_ups, agent_notes) where agent_notes are
    {person, project, text, evidence} for attachment to status rows.
    Escalation wins: a heads-up that restates an escalation is dropped.
    """
    escalations: list[dict] = []
    heads_ups: list[dict] = []
    agent_notes: list[dict] = []

    for key in SIGNAL_KEYS:
        slot = signals.get(key) or {}
        for f in slot.get("findings") or []:
            kind = (f.get("kind") or _infer_kind(key)).lower()
            ev_line = _fmt_evidence_line(f.get("evidence") or [])
            if kind == "escalation":
                escalations.append(
                    {
                        "text": f["text"],
                        "evidence": ev_line,
                        "why_ranked_here": f.get("why_ranked_here") or "time-sensitive",
                        "who": f.get("who"),
                        "project": f.get("project"),
                        "agent_note": f.get("agent_note"),
                        "_rule": key,
                    }
                )
            elif kind == "heads_up":
                heads_ups.append(
                    {
                        "text": f["text"],
                        "evidence": ev_line,
                        "project": f.get("project"),
                        "who": f.get("who"),
                        "_rule": key,
                    }
                )
            else:
                agent_notes.append(
                    {
                        "person": f.get("who") or "",
                        "project": f.get("project") or "",
                        "text": f.get("agent_note") or f["text"],
                        "evidence": ev_line,
                        "_rule": key,
                    }
                )

    held = 0
    if len(escalations) > MAX_ESCALATIONS:
        held = len(escalations) - MAX_ESCALATIONS
        escalations = escalations[:MAX_ESCALATIONS]
        escalations.append(
            {
                "text": f"+{held} lower-priority items held — ask to see them.",
                "evidence": "— ranking cap",
                "why_ranked_here": "overflow after top 5",
                "who": None,
                "project": None,
                "agent_note": None,
                "_rule": "cap",
            }
        )
    if held:
        escalations = escalations[: MAX_ESCALATIONS + 1]
    else:
        for e in escalations:
            if not (e.get("why_ranked_here") or "").strip():
                e["why_ranked_here"] = "time-sensitive"

    heads_ups = _dedupe_heads_ups_against_escalations(heads_ups, escalations)
    return escalations, heads_ups, agent_notes


def _dedupe_heads_ups_against_escalations(
    heads_ups: list[dict], escalations: list[dict]
) -> list[dict]:
    """Drop heads-ups that duplicate an escalation (same project/who + overlapping text)."""
    if not heads_ups or not escalations:
        return heads_ups
    kept: list[dict] = []
    for h in heads_ups:
        h_text = h.get("text") or ""
        h_proj = (h.get("project") or "").strip().lower()
        h_who = (h.get("who") or "").strip().lower()
        dup = False
        for e in escalations:
            if str(e.get("text") or "").startswith("+"):
                continue
            e_proj = (e.get("project") or "").strip().lower()
            e_who = (e.get("who") or "").strip().lower()
            same_project = bool(h_proj and e_proj and h_proj == e_proj)
            same_who = bool(h_who and e_who and h_who == e_who)
            if (same_project or same_who) and _texts_overlap(h_text, e.get("text") or ""):
                dup = True
                break
            # Exact fingerprint match
            if _overlap_key(h_text, h.get("project"), h.get("who")) == _overlap_key(
                e.get("text") or "", e.get("project"), e.get("who")
            ):
                dup = True
                break
        if not dup:
            kept.append(h)
    return kept


def attach_agent_notes(status_rows: list[dict], agent_notes: list[dict]) -> None:
    """Merge agent notes onto matching status person×project rows (in place)."""
    if not agent_notes:
        return
    by_key: dict[tuple[str, str], list[str]] = {}
    for n in agent_notes:
        person = (n.get("person") or "").strip()
        if not person:
            continue
        proj = (n.get("project") or "").strip()
        text = (n.get("text") or "").strip()
        if not text:
            continue
        by_key.setdefault((person.lower(), proj.lower()), []).append(text)

    for row in status_rows:
        person = (row.get("person") or "").strip()
        proj = (row.get("project") or "").strip()
        notes = by_key.get((person.lower(), proj.lower())) or []
        if not notes and proj:
            # Fall back to person-only notes.
            notes = by_key.get((person.lower(), "")) or []
        if notes:
            existing = (row.get("agent_note") or "").strip()
            merged = " · ".join(notes)
            row["agent_note"] = f"{existing} · {merged}" if existing else merged


def enforce_intelligence_artifacts(digest: dict) -> None:
    """Normalize escalations / heads_ups on the Pass B digest; drop invalid rows."""
    kept_esc: list[dict] = []
    for e in digest.get("escalations") or []:
        if not isinstance(e, dict):
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        if blocklist_hits(text):
            continue
        ev = e.get("evidence")
        if isinstance(ev, list):
            ev = _fmt_evidence_line(_normalize_evidence(ev))
        ev = (ev or "").strip()
        if not ev:
            continue
        kept_esc.append(
            {
                "text": text,
                "evidence": ev if ev.startswith("—") or ev.startswith("-") else f"— {ev}",
                "why_ranked_here": (e.get("why_ranked_here") or "time-sensitive").strip(),
                "who": (e.get("who") or "").strip() or None,
                "project": (e.get("project") or "").strip() or None,
                "agent_note": (e.get("agent_note") or "").strip() or None,
            }
        )
    if len(kept_esc) > MAX_ESCALATIONS:
        n_held = len(kept_esc) - MAX_ESCALATIONS
        kept_esc = kept_esc[:MAX_ESCALATIONS]
        kept_esc.append(
            {
                "text": f"+{n_held} lower-priority items held — ask to see them.",
                "evidence": "— ranking cap",
                "why_ranked_here": "overflow after top 5",
                "who": None,
                "project": None,
                "agent_note": None,
            }
        )
    digest["escalations"] = kept_esc

    kept_hu: list[dict] = []
    for h in digest.get("heads_ups") or []:
        if not isinstance(h, dict):
            continue
        text = (h.get("text") or "").strip()
        if not text or blocklist_hits(text):
            continue
        ev = h.get("evidence")
        if isinstance(ev, list):
            ev = _fmt_evidence_line(_normalize_evidence(ev))
        ev = (ev or "").strip()
        if not ev:
            continue
        kept_hu.append(
            {
                "text": text,
                "evidence": ev if ev.startswith("—") or ev.startswith("-") else f"— {ev}",
                "project": (h.get("project") or "").strip() or None,
                "who": (h.get("who") or "").strip() or None,
            }
        )
    digest["heads_ups"] = _dedupe_heads_ups_against_escalations(kept_hu, kept_esc)

    for row in digest.get("status") or []:
        note = (row.get("agent_note") or "").strip()
        if note and blocklist_hits(note):
            row.pop("agent_note", None)
        elif note:
            row["agent_note"] = note
        else:
            row.pop("agent_note", None)
