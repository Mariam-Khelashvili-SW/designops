"""Call summary → client email draft pipeline.

Ported from transcript-processor pilot. Anthropic for LLM; drafts stored in designops DB.
NEVER sends email.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from designops.adapters.llm import LLMClient, LLMResult, parse_digest_json
from designops.adapters.transcript_api import get_transcript
from designops.core.models import CallSummaryDraft, Person, Pipeline
from designops.pipelines.call_scope import is_external_call, is_internal_email

log = logging.getLogger(__name__)

PIPELINE_KEY = "call-summary"
SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "call-summary.md"

EXTRACTION_SYSTEM = """You extract structured facts from a client call transcript for a design agency
(scandiweb). Your output is JSON matching the provided schema. Rules:

1. EVIDENCE OR IT DIDN'T HAPPEN. Every item must carry a verbatim quote from the
   transcript in `evidence`. If you cannot quote it, do not output it.
2. Auto-transcripts are often garbled. A fragment like "configurator" or
   "estimated shipping" proves a topic was DISCUSSED, not what was DECIDED.
   Topics with unclear outcomes go in `topics_discussed_unclear_outcome`, never
   in `decisions`.
3. A decision requires clear agreement language from the CLIENT side or an
   unambiguous statement of outcome. When in doubt, downgrade: decision →
   needs_client_approval → topic_discussed.
   Capture decisions at CHANGE-POINT granularity — the specific update that
   was agreed, not summaries.
4. Dates: only copy dates that literally appear. Never infer a date from context.
5. Artifact mentions: record Figma/Notion/staging references; URL only if verbatim.
6. Distinguish client vs internal (scandiweb) attendees; if unsure, list under internal.
7. If heavily degraded, set transcript_quality = "degraded" and be maximally conservative.

Respond with a single JSON object only (no markdown fences), matching this shape:
{
  "meeting": {"title": "", "date": "", "attendees_client": [], "attendees_internal": []},
  "transcript_quality": "good" | "degraded",
  "decisions": [{"text": "", "confidence": "high|low", "evidence": ""}],
  "needs_client_approval": [{"text": "", "blocking": true, "confidence": "", "evidence": ""}],
  "next_steps": [{"text": "", "owner": "us|client", "date": null, "confidence": "", "evidence": ""}],
  "next_meeting": {"date": null, "evidence": ""},
  "artifacts_mentioned": [{"name": "", "platform": "", "explicit_url": null, "evidence": ""}],
  "topics_discussed_unclear_outcome": [{"topic": "", "evidence": ""}]
}
"""

COMPOSITION_SYSTEM = """You write a post-call confirmation email to a client on behalf of a scandiweb
account owner, from a validated JSON fact sheet. You are drafting; a human reviews and sends.

VOICE & FRAME
- Warm, concise, professional. No agency jargon.
- Frame as confirmation of shared understanding — never as new commitments.
- Sign off with the account owner's name only.

STRUCTURE (house format — keep these sections, in this order; omit empty sections):
  1. Opening: "Thank you for joining the call today. A quick summary of what we covered:"
  2. **What we aligned on** — exact agreed change points (+ agreed next call if any)
  3. **Pending items from scandiweb** — deliverables we owe
  4. **Pending items from your side** — what the client owes
  5. **Next steps** — only if not already covered by pending lists
  6. Sign-off

NO DUPLICATION. Every item in exactly one section.

HARD RULES
- SENDABLE AS-IS. No meta-placeholders like "[CONFIRM: …]". Only allowed brackets:
  "[Figma link]", "[link]", "[date]", "[name]".
- Low-confidence / unclear items go in reviewer_notes, not the body.
- Use ONLY the fact sheet. Never invent prices, hours, budgets, or scope changes.
- Subject: "<Project> — summary of our call, <date>".
- Output JSON only: {"subject": "", "body": "", "reviewer_notes": []}.
  Body is plain text with **section labels**; no HTML.
"""


# ─── Designer config ──────────────────────────────────────────────────────────


def get_call_summary_pipeline(session: Session) -> Pipeline:
    pipe = session.query(Pipeline).filter_by(key=PIPELINE_KEY).one_or_none()
    if pipe is None:
        raise RuntimeError("call-summary pipeline not bootstrapped — restart app or seed pipelines")
    return pipe


def default_designer_config() -> dict:
    return {"include_roles": ["Designer"], "manual_emails": [], "selected_person_ids": []}


def load_designer_config(session: Session) -> dict:
    pipe = get_call_summary_pipeline(session)
    cfg = dict(pipe.config or {})
    base = default_designer_config()
    base.update({k: cfg[k] for k in base if k in cfg})
    if not isinstance(base["include_roles"], list):
        base["include_roles"] = ["Designer"]
    if not isinstance(base["manual_emails"], list):
        base["manual_emails"] = []
    if not isinstance(base["selected_person_ids"], list):
        base["selected_person_ids"] = []
    return base


def save_designer_config(session: Session, config: dict) -> dict:
    pipe = get_call_summary_pipeline(session)
    merged = default_designer_config()
    merged["include_roles"] = [
        str(r).strip() for r in (config.get("include_roles") or []) if str(r).strip()
    ] or ["Designer"]
    emails: list[str] = []
    seen: set[str] = set()
    for e in config.get("manual_emails") or []:
        email = str(e).strip().lower()
        if email and "@" in email and email not in seen:
            seen.add(email)
            emails.append(email)
    merged["manual_emails"] = emails
    ids: list[str] = []
    id_seen: set[str] = set()
    for pid in config.get("selected_person_ids") or []:
        s = str(pid).strip()
        if s and s not in id_seen:
            id_seen.add(s)
            ids.append(s)
    merged["selected_person_ids"] = ids
    pipe.config = {**(pipe.config or {}), **merged}
    session.add(pipe)
    session.commit()
    return merged


def _role_matches(person_role: str | None, include_roles: list[str]) -> bool:
    if not person_role or not include_roles:
        return False
    role_l = person_role.lower()
    return any(r.lower() in role_l for r in include_roles if r)


def resolve_designer_emails(session: Session) -> list[dict[str, str | None]]:
    """Return [{email, name}] for designers from roster roles ∪ selected people ∪ manual emails."""
    cfg = load_designer_config(session)
    people = session.query(Person).filter(Person.status != "out").all()
    selected = set(cfg.get("selected_person_ids") or [])
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()

    for p in people:
        include = str(p.id) in selected or _role_matches(p.role, cfg.get("include_roles") or [])
        if not include:
            continue
        for email in p.emails or []:
            e = (email or "").strip().lower()
            if not e or e in seen:
                continue
            seen.add(e)
            out.append({"email": e, "name": p.full_name})

    for email in cfg.get("manual_emails") or []:
        e = (email or "").strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        out.append({"email": e, "name": None})

    return out


# ─── Quality / evidence / policy (pure) ───────────────────────────────────────


@dataclass(slots=True)
class QualityGateResult:
    low_confidence: bool
    mean_words_per_utterance: float
    fraction_short_utterances: float
    utterance_count: int


def split_utterances(transcript: str) -> list[str]:
    return [ln.strip() for ln in transcript.splitlines() if ln.strip()]


def assess_transcript_quality(transcript: str) -> QualityGateResult:
    utterances = split_utterances(transcript)
    if not utterances:
        return QualityGateResult(True, 0.0, 1.0, 0)
    lengths = [len(u.split()) for u in utterances]
    mean = sum(lengths) / len(lengths)
    short = sum(1 for n in lengths if n < 4)
    fraction_short = short / len(lengths)
    return QualityGateResult(
        low_confidence=fraction_short > 0.6 or mean < 6,
        mean_words_per_utterance=mean,
        fraction_short_utterances=fraction_short,
        utterance_count=len(utterances),
    )


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def dice_coefficient(a: str, b: str) -> float:
    x, y = _normalize(a), _normalize(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if len(x) < 2 or len(y) < 2:
        return 1.0 if x == y else 0.0

    def bigrams(s: str) -> dict[str, int]:
        m: dict[str, int] = {}
        for i in range(len(s) - 1):
            bg = s[i : i + 2]
            m[bg] = m.get(bg, 0) + 1
        return m

    a_map, b_map = bigrams(x), bigrams(y)
    intersection = sum(min(c, b_map.get(bg, 0)) for bg, c in a_map.items())
    return (2 * intersection) / (len(x) - 1 + (len(y) - 1))


def evidence_matches_transcript(evidence: str, transcript: str, threshold: float = 0.85) -> bool:
    ev = _normalize(evidence)
    if not ev:
        return False
    src = _normalize(transcript)
    if ev in src:
        return True
    window = max(len(ev), 8)
    if len(src) < window:
        return dice_coefficient(ev, src) >= threshold
    step = max(1, window // 4)
    best = 0.0
    for i in range(0, len(src) - min(window, len(src)) + 1, step):
        best = max(best, dice_coefficient(ev, src[i : i + window]))
        if best >= threshold:
            return True
    if len(ev) < len(src):
        step2 = max(1, len(ev) // 3)
        for i in range(0, len(src) - len(ev) + 1, step2):
            best = max(best, dice_coefficient(ev, src[i : i + len(ev)]))
            if best >= threshold:
                return True
    return best >= threshold


def _keep_if_evidence(items: list[dict], transcript: str) -> tuple[list[dict], int]:
    kept: list[dict] = []
    dropped = 0
    for item in items:
        if evidence_matches_transcript(str(item.get("evidence") or ""), transcript):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def validate_extraction_evidence(extraction: dict, transcript: str) -> tuple[dict, int]:
    dropped = 0
    d1, n = _keep_if_evidence(list(extraction.get("decisions") or []), transcript)
    dropped += n
    d2, n = _keep_if_evidence(list(extraction.get("needs_client_approval") or []), transcript)
    dropped += n
    d3, n = _keep_if_evidence(list(extraction.get("next_steps") or []), transcript)
    dropped += n
    d4, n = _keep_if_evidence(list(extraction.get("artifacts_mentioned") or []), transcript)
    dropped += n
    d5, n = _keep_if_evidence(list(extraction.get("topics_discussed_unclear_outcome") or []), transcript)
    dropped += n

    next_meeting = dict(extraction.get("next_meeting") or {"date": None, "evidence": ""})
    if next_meeting.get("date") or next_meeting.get("evidence"):
        ev = str(next_meeting.get("evidence") or "")
        if ev and not evidence_matches_transcript(ev, transcript):
            next_meeting = {"date": None, "evidence": ""}
            dropped += 1
        elif next_meeting.get("date") and _normalize(str(next_meeting["date"])) not in _normalize(
            transcript
        ):
            next_meeting = {**next_meeting, "date": None}
            dropped += 1

    next_steps = []
    for step in d3:
        date = step.get("date")
        if date and _normalize(str(date)) not in _normalize(transcript):
            dropped += 1
            next_steps.append({**step, "date": None})
        else:
            next_steps.append(step)

    return {
        **extraction,
        "decisions": d1,
        "needs_client_approval": d2,
        "next_steps": next_steps,
        "next_meeting": next_meeting,
        "artifacts_mentioned": d4,
        "topics_discussed_unclear_outcome": d5,
    }, dropped


URL_REGEX = re.compile(
    r"https?://(?:www\.)?(?:figma\.com|notion\.so|notion\.site|[\w.-]+\.[\w.-]+)[^\s\]\)\"']*",
    re.I,
)


def extract_explicit_urls(transcript: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_REGEX.findall(transcript or ""):
        cleaned = re.sub(r"[.,;:]+$", "", m)
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def build_link_map(artifacts: list[dict], explicit_urls: list[str]) -> dict[str, str]:
    link_map: dict[str, str] = {u: u for u in explicit_urls}
    for art in artifacts:
        name = str(art.get("name") or "")
        url = art.get("explicit_url")
        if url and url in explicit_urls:
            link_map[name] = str(url)
        elif url and re.match(r"^https?://", str(url), re.I):
            link_map[name] = str(url)
        else:
            platform = str(art.get("platform") or "").lower()
            link_map[name] = "[Figma link]" if "figma" in platform else "[link]"
    return link_map


def load_project_link_registry(session: Session | None = None) -> dict:
    """Project link registry (spec §5): Figma / Notion URLs per project.

    Sources (later wins): seeds/call_summary_links.yaml, then Pipeline.config['link_registry'].
    """
    import yaml

    registry: dict = {"projects": {}}
    seed = Path(__file__).resolve().parents[1] / "seeds" / "call_summary_links.yaml"
    if seed.is_file():
        try:
            data = yaml.safe_load(seed.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                registry = data
        except Exception:  # noqa: BLE001
            log.warning("Failed to load %s", seed)
    if session is not None:
        try:
            pipe = get_call_summary_pipeline(session)
            cfg_reg = (pipe.config or {}).get("link_registry")
            if isinstance(cfg_reg, dict) and cfg_reg.get("projects"):
                # Shallow merge projects
                base = dict(registry.get("projects") or {})
                base.update(cfg_reg["projects"])
                registry = {**registry, "projects": base}
        except Exception:  # noqa: BLE001
            pass
    return registry


def apply_registry_to_link_map(
    link_map: dict[str, str],
    *,
    project_name: str,
    artifacts: list[dict],
    registry: dict,
    jira_key: str | None = None,
) -> dict[str, str]:
    """Fill [Figma link]/[link] placeholders from project registry by fuzzy name match.

    Prefer an exact ``jira_key`` entry (e.g. SGDB2B) when the call's board is known.
    """
    projects = registry.get("projects") if isinstance(registry, dict) else None
    if not isinstance(projects, dict) or not projects:
        return link_map

    project_key = None
    if jira_key:
        jk = jira_key.strip().upper()
        for key in projects:
            if str(key).strip().upper() == jk:
                project_key = key
                break
    if project_key is None:
        pname = (project_name or "").lower()
        for key in projects:
            if str(key).lower() in pname or pname in str(key).lower():
                project_key = key
                break
    if project_key is None:
        return link_map

    entry = projects.get(project_key) or {}
    figma_entries = entry.get("figma") if isinstance(entry, dict) else None
    notion_brief = entry.get("notion_brief") if isinstance(entry, dict) else None
    out = dict(link_map)

    if isinstance(figma_entries, list):
        for art in artifacts:
            name = str(art.get("name") or "")
            current = out.get(name, "")
            if current and current.startswith("http"):
                continue
            name_l = name.lower()
            best_url = None
            for fe in figma_entries:
                if not isinstance(fe, dict):
                    continue
                fe_name = str(fe.get("name") or "").lower()
                fe_url = fe.get("url")
                if not fe_url:
                    continue
                if fe_name and (fe_name in name_l or name_l in fe_name or dice_coefficient(fe_name, name_l) >= 0.5):
                    best_url = str(fe_url)
                    break
            if best_url:
                out[name] = best_url
            elif not current and str(art.get("platform") or "").lower().find("figma") >= 0:
                if len(figma_entries) == 1 and isinstance(figma_entries[0], dict) and figma_entries[0].get("url"):
                    out[name] = str(figma_entries[0]["url"])

    if notion_brief and isinstance(notion_brief, str) and notion_brief.startswith("http"):
        out.setdefault("notion_brief", notion_brief)
        for art in artifacts:
            name = str(art.get("name") or "")
            plat = str(art.get("platform") or "").lower()
            name_l = name.lower()
            if (
                "notion" in plat
                or "notion" in name_l
                or ("road" in name_l and "map" in name_l)
                or "brief" in name_l
            ):
                if not out.get(name, "").startswith("http"):
                    out[name] = notion_brief

    return out


def prune_older_drafts(session: Session, transcript_id: str | None = None) -> int:
    """Keep only the newest draft per transcript_id. Returns number deleted."""
    q = session.query(CallSummaryDraft)
    if transcript_id:
        q = q.filter(CallSummaryDraft.transcript_id == transcript_id)
    rows = q.order_by(CallSummaryDraft.generated_at.desc()).all()
    seen: set[str] = set()
    deleted = 0
    for d in rows:
        tid = d.transcript_id
        if tid in seen:
            session.delete(d)
            deleted += 1
        else:
            seen.add(tid)
    if deleted:
        session.commit()
    return deleted


def latest_draft_ids_by_transcript(session: Session) -> dict[str, str]:
    """transcript_id → latest draft id (uuid string)."""
    rows = (
        session.query(CallSummaryDraft)
        .order_by(CallSummaryDraft.generated_at.desc())
        .all()
    )
    out: dict[str, str] = {}
    for d in rows:
        if d.transcript_id not in out:
            out[d.transcript_id] = str(d.id)
    return out


ALLOWED_PLACEHOLDER_RE = re.compile(r"\[(Figma link|link|date|name)\]", re.I)
ANY_BRACKET_RE = re.compile(r"\[[^\]]+\]")
CURRENCY_RE = re.compile(
    r"(?:€|£|\$|USD|EUR|GBP)\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:€|£|\$|USD|EUR|GBP)",
    re.I,
)
EFFORT_RE = re.compile(r"\b\d+\s*(?:h|hours?|days?)\b", re.I)
SCOPE_EXPANSION_RE = re.compile(r"\bwe agree to add\b|\bwe will additionally\b", re.I)


def count_placeholders(body: str) -> int:
    return len(ALLOWED_PLACEHOLDER_RE.findall(body or ""))


def find_disallowed_placeholders(body: str) -> list[str]:
    return [p for p in ANY_BRACKET_RE.findall(body or "") if not re.match(r"^\[(Figma link|link|date|name)\]$", p, re.I)]


def run_policy_guard(
    *,
    body: str,
    transcript_quality: str,
    source_text: str,
    allowed_urls: list[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    src_norm = _normalize(source_text)

    m = CURRENCY_RE.search(body or "")
    if m and _normalize(m.group(0)) not in src_norm:
        reasons.append(f"Currency/amount not in source: {m.group(0)}")
    m = EFFORT_RE.search(body or "")
    if m and _normalize(m.group(0)) not in src_norm:
        reasons.append(f"Effort estimate not in source: {m.group(0)}")
    if SCOPE_EXPANSION_RE.search(body or ""):
        reasons.append("Scope-expansion phrasing detected")
    disallowed = find_disallowed_placeholders(body or "")
    if disallowed:
        reasons.append(f"Disallowed placeholders: {', '.join(disallowed)}")
    allow = {u.lower() for u in allowed_urls}
    for u in re.findall(r"https?://[^\s\]\)\"']+", body or "", flags=re.I):
        cleaned = re.sub(r"[.,;:]+$", "", u)
        if cleaned.lower() not in allow:
            reasons.append(f"URL not on allowlist: {cleaned}")
    if transcript_quality == "degraded" and count_placeholders(body or "") == 0:
        reasons.append("Degraded transcript produced a placeholder-free draft")
    return (len(reasons) == 0, reasons)


def skeleton_composition(
    *,
    project_name: str,
    date_label: str,
    owner_name: str,
    reviewer_notes: list[str] | None = None,
) -> dict[str, Any]:
    subject = f"{project_name} — summary of our call, {date_label or '[date]'}"
    body = "\n".join(
        [
            "Thank you for joining the call today.",
            "A quick summary of what we covered:",
            "",
            "**What we aligned on**",
            "- Follow-up details to be confirmed ([date])",
            "",
            owner_name,
        ]
    )
    return {
        "subject": subject,
        "body": body,
        "reviewer_notes": list(reviewer_notes or [])
        + ["Skeleton draft: fact sheet was empty or policy blocked regeneration."],
    }


def display_name_from_email(email: str) -> str:
    local = (email.split("@")[0] if email else "") or email
    return " ".join(p[:1].upper() + p[1:] for p in re.split(r"[._\-]+", local) if p)


def pick_owner_name(
    designers_on_call: list[dict[str, str | None]],
    organizer_email: str | None,
    attendees: list[dict],
) -> str:
    for d in designers_on_call:
        if d.get("name") and str(d["name"]).strip():
            return str(d["name"]).strip()
        if d.get("email"):
            return display_name_from_email(str(d["email"]))
    if organizer_email:
        for a in attendees:
            if (a.get("email") or "").lower() == organizer_email.lower() and (a.get("name") or "").strip():
                return str(a["name"]).strip()
        return display_name_from_email(organizer_email)
    return "scandiweb"


def client_display_names(attendees: list[dict], client_participants: list[str] | None) -> str:
    client_set = {e.lower() for e in (client_participants or [])}
    names: list[str] = []
    for a in attendees:
        email = (a.get("email") or "").lower()
        if not email:
            continue
        is_client = email in client_set if client_set else not is_internal_email(email)
        if not is_client:
            continue
        names.append((a.get("name") or "").strip() or display_name_from_email(email))
    return ", ".join(names)


def _empty_extraction() -> dict:
    return {
        "meeting": {"title": "", "date": "", "attendees_client": [], "attendees_internal": []},
        "transcript_quality": "degraded",
        "decisions": [],
        "needs_client_approval": [],
        "next_steps": [],
        "next_meeting": {"date": None, "evidence": ""},
        "artifacts_mentioned": [],
        "topics_discussed_unclear_outcome": [],
    }


def _llm_json(system: str, user: str, client: LLMClient) -> tuple[dict, LLMResult]:
    result = client.synthesize(system=system, user_content=user, max_tokens=8000)
    return parse_digest_json(result.text), result


# ─── Orchestration ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class GenerateResult:
    draft_id: uuid.UUID
    subject: str
    body_text: str
    reviewer_notes: list[str]
    low_confidence: bool
    transcript_quality: str
    placeholder_count: int
    policy_blocked: bool
    policy_block_reason: str | None


def generate_call_summary_draft(
    session: Session,
    transcript_id: str,
    *,
    llm: LLMClient | None = None,
) -> GenerateResult:
    designers = resolve_designer_emails(session)
    designer_set = {str(d["email"]).lower() for d in designers if d.get("email")}

    item = get_transcript(transcript_id)
    content = str(item.get("content") or "")
    if not content.strip():
        raise RuntimeError("Transcript has no content")

    attendees = item.get("attendees") if isinstance(item.get("attendees"), list) else []
    attendees = [a for a in attendees if isinstance(a, dict)]
    client_participants = item.get("clientParticipants")
    if not isinstance(client_participants, list):
        client_participants = []
    client_participants = [str(e) for e in client_participants]

    if not is_external_call(attendees, client_participants):
        raise RuntimeError("Transcript is not an external (client) call")

    designers_on_call = [
        d
        for d in designers
        if d.get("email")
        and any((a.get("email") or "").lower() == str(d["email"]).lower() for a in attendees)
    ]
    designer_recipient_emails = [str(d["email"]) for d in designers_on_call if d.get("email")]
    if not designer_recipient_emails:
        for a in attendees:
            e = (a.get("email") or "").lower()
            if e in designer_set:
                designer_recipient_emails.append(e)

    attendees_internal = [
        (a.get("name") or a.get("email") or "")
        for a in attendees
        if a.get("email") and is_internal_email(str(a["email"]))
    ]
    attendees_client = []
    for a in attendees:
        email = a.get("email")
        if not email:
            continue
        if client_participants:
            if email.lower() in {e.lower() for e in client_participants}:
                attendees_client.append(a.get("name") or email)
        elif not is_internal_email(str(email)):
            attendees_client.append(a.get("name") or email)

    quality = assess_transcript_quality(content)
    meeting_date = ""
    if item.get("eventStartTime"):
        meeting_date = str(item["eventStartTime"])[:10]
    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    project_name = (account or {}).get("name") or item.get("name") or "Project"
    owner_name = pick_owner_name(designers_on_call, item.get("organizerEmail"), attendees)
    client_names = client_display_names(attendees, client_participants)

    client = llm or LLMClient()
    total_in = total_out = 0
    total_cost = 0.0

    skill_extra = ""
    if SKILL_PATH.is_file():
        skill_extra = "\n\n" + SKILL_PATH.read_text(encoding="utf-8")[:4000]

    low_note = ""
    if quality.low_confidence:
        low_note = (
            "\n\nQUALITY GATE: low-confidence transcript. Be maximally conservative; "
            'prefer topics_discussed_unclear_outcome; set transcript_quality to "degraded".'
        )

    user_extract = (
        f"Meeting title: {item.get('name') or ''}\n"
        f"Meeting date: {meeting_date or '(unknown)'}\n"
        f"Internal attendees: {', '.join(attendees_internal) or '(none listed)'}\n"
        f"Client attendees: {', '.join(attendees_client) or '(none listed)'}\n"
        f"{low_note}\n\nTranscript:\n{content}"
    )

    registry = load_project_link_registry(session)
    from designops.pipelines.call_summary_links import (
        apply_jira_fairwind_to_link_map,
        find_account_for_project,
        inject_resolved_urls_into_body,
        resolve_call_jira_key,
    )

    meeting_title = str(item.get("name") or "")
    call_jira_key: str | None = None
    acct_early = find_account_for_project(session, str(project_name))
    if acct_early is not None:
        call_jira_key = resolve_call_jira_key(
            list(acct_early.jira_project_keys or []),
            meeting_title=meeting_title,
        )

    # Early Notion brief URL for extraction context (page body via Notion API = later)
    early_links = apply_registry_to_link_map(
        {},
        project_name=str(project_name),
        artifacts=[],
        registry=registry,
        jira_key=call_jira_key,
    )
    if early_links.get("notion_brief"):
        user_extract += (
            "\n\nNotion project brief URL (link only — do not invent page content):\n"
            + early_links["notion_brief"]
        )

    try:
        extraction, r1 = _llm_json(EXTRACTION_SYSTEM + skill_extra, user_extract, client)
        total_in += r1.input_tokens
        total_out += r1.output_tokens
        total_cost += r1.cost_usd
    except Exception as e:  # noqa: BLE001
        log.exception("Extraction failed: %s", e)
        extraction = _empty_extraction()

    if quality.low_confidence:
        extraction["transcript_quality"] = "degraded"

    extraction, dropped = validate_extraction_evidence(extraction, content)
    if dropped:
        log.info("Dropped %s extraction items with unmatched evidence (%s)", dropped, transcript_id)

    explicit_urls = extract_explicit_urls(content)
    arts = []
    for art in extraction.get("artifacts_mentioned") or []:
        url = art.get("explicit_url")
        if url and url not in explicit_urls and str(url) not in content:
            arts.append({**art, "explicit_url": None})
        else:
            arts.append(art)
    extraction["artifacts_mentioned"] = arts
    link_map = build_link_map(arts, explicit_urls)
    link_map = apply_registry_to_link_map(
        link_map,
        project_name=str(project_name),
        artifacts=arts,
        registry=registry,
        jira_key=call_jira_key,
    )
    link_map, link_notes = apply_jira_fairwind_to_link_map(
        link_map,
        session=session,
        project_name=str(project_name),
        artifacts=arts,
        meeting_title=meeting_title,
    )
    call_jira_key = link_map.pop("_call_jira_key", call_jira_key) or call_jira_key
    link_map.pop("_project_figma", None)

    allowed_urls = list(explicit_urls) + [
        v for v in link_map.values() if isinstance(v, str) and re.match(r"^https?://", v, re.I)
    ]
    fact_empty = (
        not extraction.get("decisions")
        and not extraction.get("needs_client_approval")
        and not extraction.get("next_steps")
        and not (extraction.get("next_meeting") or {}).get("date")
        and not extraction.get("topics_discussed_unclear_outcome")
        and not extraction.get("artifacts_mentioned")
    )

    if fact_empty:
        composition = skeleton_composition(
            project_name=str(project_name),
            date_label=meeting_date or str((extraction.get("meeting") or {}).get("date") or "[date]"),
            owner_name=owner_name,
            reviewer_notes=["Empty fact sheet after extraction/validation — skeleton draft only."],
        )
    else:
        user_comp = (
            f"Account owner (sender): {owner_name}\n"
            f"Client contact(s): {client_names or '[name]'}\n"
            f"Project: {project_name}\n"
            f"Fact sheet:\n{json.dumps(extraction, indent=2)}\n"
            f"Resolved links:\n{json.dumps(link_map, indent=2)}"
        )
        try:
            composition, r2 = _llm_json(COMPOSITION_SYSTEM, user_comp, client)
            total_in += r2.input_tokens
            total_out += r2.output_tokens
            total_cost += r2.cost_usd
            composition.setdefault("reviewer_notes", [])
        except Exception as e:  # noqa: BLE001
            log.exception("Composition failed: %s", e)
            composition = skeleton_composition(
                project_name=str(project_name),
                date_label=meeting_date or "[date]",
                owner_name=owner_name,
                reviewer_notes=[f"Composition LLM failed: {e}"],
            )

    source_for_policy = json.dumps(extraction) + "\n" + content
    tq = str(extraction.get("transcript_quality") or "good")
    ok, reasons = run_policy_guard(
        body=str(composition.get("body") or ""),
        transcript_quality=tq,
        source_text=source_for_policy,
        allowed_urls=allowed_urls,
    )

    policy_blocked = False
    policy_block_reason: str | None = None

    if not ok and not fact_empty:
        log.warning("Policy guard failed (%s); regenerating once", reasons)
        user_comp = (
            f"Account owner (sender): {owner_name}\n"
            f"Client contact(s): {client_names or '[name]'}\n"
            f"Project: {project_name}\n"
            f"Fact sheet:\n{json.dumps(extraction, indent=2)}\n"
            f"Resolved links:\n{json.dumps(link_map, indent=2)}\n"
            f"Previous draft failed policy: {'; '.join(reasons)}. Fix and regenerate."
        )
        try:
            composition, r3 = _llm_json(COMPOSITION_SYSTEM, user_comp, client)
            total_in += r3.input_tokens
            total_out += r3.output_tokens
            total_cost += r3.cost_usd
            composition.setdefault("reviewer_notes", [])
        except Exception:
            pass
        ok, reasons = run_policy_guard(
            body=str(composition.get("body") or ""),
            transcript_quality=tq,
            source_text=source_for_policy,
            allowed_urls=allowed_urls,
        )

    if not ok:
        policy_blocked = True
        policy_block_reason = "; ".join(reasons)
        composition = skeleton_composition(
            project_name=str(project_name),
            date_label=meeting_date or str((extraction.get("meeting") or {}).get("date") or "[date]"),
            owner_name=owner_name,
            reviewer_notes=[f"Policy guard blocked draft: {policy_block_reason}"]
            + list(composition.get("reviewer_notes") or []),
        )

    if tq == "degraded":
        notes = list(composition.get("reviewer_notes") or [])
        warn = "Transcript quality degraded — verify against your own notes before sending."
        if warn not in notes:
            notes.append(warn)
        composition["reviewer_notes"] = notes

    # Deterministic: swap [link] / [Figma link] for resolved URLs on matching lines
    body_final = inject_resolved_urls_into_body(
        str(composition.get("body") or ""),
        link_map,
        arts,
    )
    composition["body"] = body_final

    reviewer_notes = list(composition.get("reviewer_notes") or [])
    for n in link_notes:
        if n and n not in reviewer_notes:
            reviewer_notes.append(n)
    if call_jira_key:
        scope_note = f"Artifact links looked up in Jira project {call_jira_key} only (from call title)."
        if scope_note not in reviewer_notes:
            reviewer_notes.insert(0, scope_note)
    composition["reviewer_notes"] = reviewer_notes

    placeholder_count = count_placeholders(str(composition.get("body") or ""))
    reviewer_notes = list(composition.get("reviewer_notes") or [])

    # One draft per call: drop older rows for this transcript before insert
    prune_older_drafts(session, transcript_id=str(item["id"]))

    draft = CallSummaryDraft(
        transcript_id=str(item["id"]),
        transcript_name=item.get("name"),
        account_name=(account or {}).get("name") if account else None,
        subject=str(composition.get("subject") or "(no subject)"),
        body_text=str(composition.get("body") or ""),
        reviewer_notes=reviewer_notes,
        extraction_json=extraction,
        transcript_quality=tq,
        low_confidence=quality.low_confidence,
        placeholder_count=placeholder_count,
        designer_recipient_emails=designer_recipient_emails,
        owner_name=owner_name,
        policy_blocked=policy_blocked,
        policy_block_reason=policy_block_reason,
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=round(total_cost, 4),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)

    return GenerateResult(
        draft_id=draft.id,
        subject=draft.subject,
        body_text=draft.body_text,
        reviewer_notes=reviewer_notes,
        low_confidence=quality.low_confidence,
        transcript_quality=tq,
        placeholder_count=placeholder_count,
        policy_blocked=policy_blocked,
        policy_block_reason=policy_block_reason,
    )


def list_matching_calls(
    session: Session,
    *,
    search: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Fetch slim transcripts for designer emails, filter to external, optional search."""
    from designops.adapters.transcript_api import list_transcripts

    designers = resolve_designer_emails(session)
    emails = [str(d["email"]) for d in designers if d.get("email")]
    if not emails:
        return [], 0

    designer_set = {e.lower() for e in emails}
    # Page through TP API; filter external + designer attendees; apply search; then slice
    collected: list[dict] = []
    api_offset = 0
    page_size = 100
    q = (search or "").strip().lower()

    while True:
        data = list_transcripts(
            participant_emails=emails,
            include_content=False,
            include_unscored=True,
            exclude_internal=True,
            limit=page_size,
            offset=api_offset,
        )
        items = data.get("items") or []
        if not items:
            break
        for t in items:
            attendees = t.get("attendees") if isinstance(t.get("attendees"), list) else []
            attendees = [a for a in attendees if isinstance(a, dict)]
            cps = t.get("clientParticipants") if isinstance(t.get("clientParticipants"), list) else []
            if not is_external_call(attendees, cps):
                continue
            designer_attendees = []
            for a in attendees:
                e = (a.get("email") or "").lower()
                if e in designer_set:
                    designer_attendees.append(
                        {"email": e, "name": (a.get("name") or a.get("resolvedName") or None)}
                    )
            if not designer_attendees:
                continue
            account = t.get("account") if isinstance(t.get("account"), dict) else {}
            row = {
                "id": t.get("id"),
                "name": t.get("name") or "",
                "account_name": (account or {}).get("name"),
                "event_start_time": t.get("eventStartTime"),
                "designer_attendees": designer_attendees,
            }
            if q:
                hay = " ".join(
                    [
                        row["name"],
                        row["account_name"] or "",
                        " ".join(
                            f"{d.get('name') or ''} {d.get('email') or ''}" for d in designer_attendees
                        ),
                    ]
                ).lower()
                if q not in hay:
                    continue
            collected.append(row)
        pagination = data.get("pagination") or {}
        if not pagination.get("hasMore"):
            break
        api_offset = int(pagination.get("nextOffset") or api_offset + len(items))
        if api_offset > 2000:  # safety cap
            break

    total = len(collected)
    page = collected[offset : offset + limit]
    draft_map = latest_draft_ids_by_transcript(session)
    for row in page:
        row["draft_id"] = draft_map.get(str(row.get("id") or ""))
    return page, total
