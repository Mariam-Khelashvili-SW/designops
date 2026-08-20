"""Call summary → client follow-up email draft pipeline (v3).

Extract → critic → compose. Anthropic for LLM; drafts stored in designops DB.
NEVER sends email. Prompts default to v3; admins can override via Pipeline.config.
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
from designops.pipelines.call_summary_prompts import (
    COMPOSITION_SYSTEM as _DEFAULT_COMPOSITION,
    CRITIC_SYSTEM as _DEFAULT_CRITIC,
    DEFAULT_PROMPTS,
    EXTRACTION_SYSTEM as _DEFAULT_EXTRACTION,
    PROMPT_KEYS,
)

log = logging.getLogger(__name__)

PIPELINE_KEY = "call-summary"
SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "call-summary.md"

# Re-export defaults for tests / imports that still reference module-level constants.
EXTRACTION_SYSTEM = _DEFAULT_EXTRACTION
CRITIC_SYSTEM = _DEFAULT_CRITIC
COMPOSITION_SYSTEM = _DEFAULT_COMPOSITION

PIPELINE_VERSION = 3
IMPACT_RANK = {"project": 4, "multi_template": 3, "single_screen": 2, "detail": 1}
VALID_TARGET_ARRAYS = (
    "decisions",
    "our_commitments",
    "client_actions",
    "open_questions",
    "flags",
    "artifacts",
    "topics_discussed_unclear_outcome",
)
ALLOWED_BRACKET_TOKENS = frozenset(
    {"figma link", "link", "date", "name", "option 1", "option 2"}
)
BANNED_PHRASE_RE = re.compile(
    r"hope this|circling back|touching base|per my last|reach out",
    re.I,
)
EM_DASH_RE = re.compile(r"—")
SHARING_NOW_RE = re.compile(r"we are sharing", re.I)
SHARING_LATER_RE = re.compile(r"we will share|will share them", re.I)
RECAP_HEADER_RE = re.compile(
    r"we also want to confirm the main points we aligned on",
    re.I,
)
OUR_ACTIONS_HEADER_RE = re.compile(r"from our side we will proceed with", re.I)
CLIENT_ACTIONS_HEADER_RE = re.compile(r"as next steps from your side", re.I)
SHARING_SECTION_RE = re.compile(
    r"as promised|we are sharing|we are adjusting|will share them",
    re.I,
)
CALL_TIME_DEICTIC_RE = re.compile(
    r"after the call|on my screen|as you can see here|as I showed you",
    re.I,
)
# Greeting token that looks like an email local-part (tsassine, l.shedden, etc.).
EMAIL_DERIVED_GREETING_RE = re.compile(
    r"Hello [^,\n]*\b[a-z]\.?[a-z]{4,}\b",
)
IMPERATIVE_STEM_RE = re.compile(
    r"^(?:send|share|deliver|provide|prepare|create|update|confirm|review|"
    r"advise|check|add|remove|fix|include|write|draft|schedule|book)\b",
    re.I,
)
STOP_HEAD_NOUNS = frozenset(
    {
        "the",
        "a",
        "an",
        "our",
        "your",
        "their",
        "this",
        "that",
        "these",
        "those",
        "new",
        "updated",
        "adjusted",
        "revised",
        "final",
    }
)


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


def default_prompt_config() -> dict[str, str]:
    return {k: DEFAULT_PROMPTS[k] for k in PROMPT_KEYS}


def load_prompt_config(session: Session) -> dict[str, Any]:
    """Effective prompts + whether each key is overridden in Pipeline.config."""
    pipe = get_call_summary_pipeline(session)
    stored = (pipe.config or {}).get("prompts")
    stored = stored if isinstance(stored, dict) else {}
    effective: dict[str, str] = {}
    overrides: dict[str, bool] = {}
    for key in PROMPT_KEYS:
        raw = stored.get(key)
        text = str(raw).strip() if raw is not None else ""
        if text:
            effective[key] = text
            overrides[key] = True
        else:
            effective[key] = DEFAULT_PROMPTS[key]
            overrides[key] = False
    return {
        "prompts": effective,
        "overrides": overrides,
        "defaults": default_prompt_config(),
        "pipeline_version": PIPELINE_VERSION,
    }


def save_prompt_config(
    session: Session,
    prompts: dict[str, str],
    *,
    reset_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Persist prompt overrides. Empty string or keys in reset_keys clear the override."""
    pipe = get_call_summary_pipeline(session)
    cfg = dict(pipe.config or {})
    stored = dict(cfg.get("prompts") or {}) if isinstance(cfg.get("prompts"), dict) else {}
    reset = reset_keys or set()
    for key in PROMPT_KEYS:
        if key in reset:
            stored.pop(key, None)
            continue
        if key not in prompts:
            continue
        text = str(prompts.get(key) or "").strip()
        if not text or text == DEFAULT_PROMPTS[key].strip():
            stored.pop(key, None)
        else:
            stored[key] = text
    cfg["prompts"] = stored
    pipe.config = cfg
    session.add(pipe)
    session.commit()
    return load_prompt_config(session)


def resolve_system_prompts(session: Session | None = None) -> dict[str, str]:
    """Prompts used at generation time (DB overrides or v3 defaults)."""
    if session is None:
        return default_prompt_config()
    try:
        return dict(load_prompt_config(session)["prompts"])
    except Exception:  # noqa: BLE001
        log.warning("Failed to load call-summary prompt overrides; using defaults")
        return default_prompt_config()


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


def _evidence_segment_matches(ev: str, src: str, threshold: float) -> bool:
    if not ev:
        return False
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


def evidence_matches_transcript(evidence: str, transcript: str, threshold: float = 0.85) -> bool:
    """Match verbatim or stitched evidence (segments joined with \" / \")."""
    raw = (evidence or "").strip()
    if not raw:
        return False
    src = _normalize(transcript)
    # Stitched multi-turn evidence: every segment must match.
    if " / " in raw:
        parts = [p.strip() for p in raw.split(" / ") if p.strip()]
        if parts and all(
            _evidence_segment_matches(_normalize(p), src, threshold) for p in parts
        ):
            return True
    return _evidence_segment_matches(_normalize(raw), src, threshold)


def _keep_if_evidence(items: list[dict], transcript: str) -> tuple[list[dict], int]:
    kept: list[dict] = []
    dropped = 0
    for item in items:
        if evidence_matches_transcript(str(item.get("evidence") or ""), transcript):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def _strip_unverifiable_date_iso(item: dict, transcript: str) -> tuple[dict, int]:
    """Clear date_iso when it does not literally appear in the transcript."""
    dropped = 0
    date = item.get("date_iso")
    if date and _normalize(str(date)) not in _normalize(transcript):
        dropped = 1
        return {**item, "date_iso": None}, dropped
    return item, dropped


def validate_extraction_evidence(extraction: dict, transcript: str) -> tuple[dict, int]:
    dropped = 0
    arrays = (
        "decisions",
        "our_commitments",
        "client_actions",
        "open_questions",
        "flags",
        "artifacts",
        "topics_discussed_unclear_outcome",
    )
    out: dict = {**extraction}
    for key in arrays:
        kept, n = _keep_if_evidence(list(extraction.get(key) or []), transcript)
        dropped += n
        if key in ("our_commitments", "client_actions"):
            cleaned: list[dict] = []
            for item in kept:
                item2, n2 = _strip_unverifiable_date_iso(item, transcript)
                dropped += n2
                cleaned.append(item2)
            out[key] = cleaned
        else:
            out[key] = kept

    next_meeting = dict(
        extraction.get("next_meeting")
        or {"date_iso": None, "timing_verbatim": None, "evidence": ""}
    )
    if next_meeting.get("date_iso") or next_meeting.get("timing_verbatim") or next_meeting.get(
        "evidence"
    ):
        ev = str(next_meeting.get("evidence") or "")
        if ev and not evidence_matches_transcript(ev, transcript):
            next_meeting = {"date_iso": None, "timing_verbatim": None, "evidence": ""}
            dropped += 1
        else:
            next_meeting, n = _strip_unverifiable_date_iso(next_meeting, transcript)
            dropped += n
    out["next_meeting"] = next_meeting
    return out, dropped


def compute_thanks_line(*, call_datetime, send_datetime) -> str:
    """Deterministic greeting thanks line from call vs send calendar days.

    v3: days_delta must be a real non-negative int. Missing call date → generic
    thanks (no weekday guess). Negative delta raises ValueError.
    """
    from datetime import date, datetime, timezone

    def _as_date(value) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone().date()
        if isinstance(value, date):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw[:19] if "T" in raw and len(raw) >= 19 else raw[:10])
            return dt.date()
        except ValueError:
            if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    return None
            return None

    call_d = _as_date(call_datetime)
    send_d = _as_date(send_datetime) or datetime.now().astimezone().date()
    if call_d is None:
        return "Thank you for the call!"
    delta = (send_d - call_d).days
    if delta is None or delta < 0:
        raise ValueError(f"bad thanks-line delta: {delta}")
    if delta == 0:
        return "Thank you for the call earlier today!"
    if delta == 1:
        return "Thank you for the call yesterday!"
    if delta <= 6:
        return f"Thank you for the call on {call_d.strftime('%A')}!"
    return "Thank you for the call!"


def first_token(name: str | None) -> str:
    """First whitespace-separated token of a display name (never from an email)."""
    raw = (name or "").strip()
    if not raw or "@" in raw:
        return ""
    # Drop trailing punctuation / parentheticals
    token = raw.split()[0].strip(" ,.;:")
    if not token or "@" in token:
        return ""
    return token


def sender_first_name(full_name: str | None) -> str:
    """Sign-off first name only (v3: never full name)."""
    token = first_token(full_name)
    return token or "scandiweb"

def merge_critic_into_extraction(
    extraction: dict, critic: dict, transcript: str
) -> tuple[dict, list[str]]:
    """Apply critic additions/reclassifications/downgrades; evidence-gate additions."""
    notes: list[str] = []
    out = {**extraction}
    for key in VALID_TARGET_ARRAYS:
        out[key] = list(extraction.get(key) or [])

    for add in critic.get("additions") or []:
        if not isinstance(add, dict):
            continue
        target = str(add.get("target_array") or "").strip()
        if target not in VALID_TARGET_ARRAYS:
            notes.append(f"Critic addition skipped (bad target_array): {target or '(empty)'}")
            continue
        item = {k: v for k, v in add.items() if k != "target_array"}
        if not evidence_matches_transcript(str(item.get("evidence") or ""), transcript):
            notes.append(f"Critic addition dropped (no evidence match) → {target}")
            continue
        out[target].append(item)
        notes.append(f"Critic added to {target}: {item.get('text') or item.get('question') or item.get('topic') or '(item)'}")

    decisions = list(out.get("decisions") or [])
    for rec in critic.get("reclassifications") or []:
        if not isinstance(rec, dict):
            continue
        needle = _normalize(str(rec.get("item_text") or rec.get("decision_text") or ""))
        field = str(rec.get("field") or "").strip()
        to_val = rec.get("to")
        if not needle or not field:
            continue
        for i, dec in enumerate(decisions):
            if needle in _normalize(str(dec.get("text") or "")):
                decisions[i] = {**dec, field: to_val}
                notes.append(f"Critic reclassified decision.{field}: {rec.get('from')} → {to_val}")
                break

    for down in critic.get("downgrades") or []:
        if not isinstance(down, dict):
            continue
        needle = _normalize(str(down.get("item_text") or down.get("decision_text") or ""))
        to_impact = str(down.get("to_impact") or "").strip()
        if not needle or to_impact not in IMPACT_RANK:
            continue
        for i, dec in enumerate(decisions):
            if needle in _normalize(str(dec.get("text") or "")):
                decisions[i] = {**dec, "impact": to_impact}
                notes.append(f"Critic downgraded impact → {to_impact}")
                break
    out["decisions"] = decisions
    return out, notes


def _body_bullets(body: str) -> list[str]:
    bullets: list[str] = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("- "):
            bullets.append(s[2:].strip())
        elif s.startswith("* "):
            bullets.append(s[2:].strip())
    return bullets


def _section_bullets(body: str, header_re: re.Pattern[str]) -> list[str]:
    lines = (body or "").splitlines()
    collecting = False
    out: list[str] = []
    for line in lines:
        if header_re.search(line):
            collecting = True
            continue
        if collecting:
            s = line.strip()
            if not s:
                if out:
                    break
                continue
            if s.startswith("- ") or s.startswith("* "):
                out.append(s[2:].strip())
            elif out and not s[0:1].isupper() and not s.startswith("As ") and not s.startswith("From "):
                # continuation line — ignore for matching
                continue
            else:
                break
    return out


_TIMING_PHRASE_RE = re.compile(
    r"\b(?:"
    r"by the end of (?:this|next) week|"
    r"end of (?:this|next) week|"
    r"this week(?: for sure)?|"
    r"next week|"
    r"tomorrow(?: morning| afternoon)?|"
    r"today|"
    r"monday|tuesday|wednesday|thursday|friday|"
    r"this month|next month"
    r")\b",
    re.I,
)


def timing_signals(timing_verbatim: str) -> list[str]:
    """Extract short timing cues composition is expected to paste (not whole monologues)."""
    raw = (timing_verbatim or "").strip()
    if not raw:
        return []
    found = [_normalize(m) for m in _TIMING_PHRASE_RE.findall(raw)]
    # Preserve order, unique
    out: list[str] = []
    seen: set[str] = set()
    for p in found:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    if out:
        return out
    # Short relative phrase as-is
    if len(raw) <= 40:
        return [_normalize(raw)]
    return []


def timing_present_in_body(timing_verbatim: str, body: str) -> bool:
    """True when body carries the commitment's timing (formatted or verbatim)."""
    signals = timing_signals(timing_verbatim)
    if not signals:
        # Long non-parseable timing blob — do not fail the draft on it.
        return True
    bnorm = _normalize(body)
    tnorm = _normalize(timing_verbatim)
    if tnorm and tnorm in bnorm:
        return True
    return any(sig in bnorm for sig in signals)


def _action_stem(text: str) -> str:
    """Normalize an action bullet to a short stem for duplication detection."""
    t = _normalize(text)
    t = re.sub(r"\s*-\s*(?:by the end of )?(?:this|next) week.*$", "", t)
    t = re.sub(
        r"^(?:confirm|advise|review and approve|please|we will|delivering|sharing|"
        r"preparing|sending|providing|updating|creating)\s+",
        "",
        t,
    )
    words = [w for w in t.split() if w not in STOP_HEAD_NOUNS]
    return " ".join(words[:4])


def _head_noun(text: str) -> str:
    words = [w for w in _normalize(text).split() if w not in STOP_HEAD_NOUNS and len(w) > 2]
    return words[0] if words else ""


def _sharing_section_text(body: str) -> str:
    """Rough slice of the sharing block (before client/our actions headers)."""
    lines = (body or "").splitlines()
    out: list[str] = []
    started = False
    for line in lines:
        if CLIENT_ACTIONS_HEADER_RE.search(line) or OUR_ACTIONS_HEADER_RE.search(line) or RECAP_HEADER_RE.search(line):
            break
        if SHARING_SECTION_RE.search(line) or started:
            started = True
            out.append(line)
    return "\n".join(out)


def validate_composition_draft(
    *,
    body: str,
    extraction: dict,
    sender_first_name: str | None = None,
    separate_email_recommended: dict | None = None,
) -> tuple[bool, list[str]]:
    """Cheap regex/code validators (v3 Step 4). Fail → retry composition."""
    reasons: list[str] = []
    text = body or ""
    if EM_DASH_RE.search(text):
        reasons.append("Em dash present in body")
    if BANNED_PHRASE_RE.search(text):
        reasons.append("Banned filler phrase in body")
    if CALL_TIME_DEICTIC_RE.search(text):
        reasons.append("Call-time deictic language in body")
    if text.count("As promised") > 1:
        reasons.append("'As promised' appears more than once")
    for token in ANY_BRACKET_RE.findall(text):
        inner = token[1:-1].strip().lower()
        if inner not in ALLOWED_BRACKET_TOKENS:
            reasons.append(f"Disallowed bracket token: {token}")

    # Greeting must not look email-derived (tsassine, lshedden, …).
    first_line = (text.split("\n")[0] if text else "").strip()
    if first_line.lower().startswith("hello") and EMAIL_DERIVED_GREETING_RE.search(first_line):
        # Allow normal Titlecase first names; flag only all-lowercase-ish tokens.
        greeting_names = first_line[len("Hello") :].strip().rstrip(",").strip()
        for part in greeting_names.split(","):
            token = part.strip()
            if token and token == token.lower() and len(token) >= 5 and " " not in token:
                reasons.append(f"Greeting contains an email-derived token: {token}")
                break

    if sender_first_name:
        last = ""
        for line in reversed(text.strip().splitlines()):
            if line.strip():
                last = line.strip()
                break
        if last and last != sender_first_name:
            reasons.append("Sign-off is not SENDER_FIRST_NAME exactly")

    # Same artifact must not appear as both shared-now and will-share.
    arts = list(extraction.get("artifacts") or [])
    sharing_block = _sharing_section_text(text)
    for art in arts:
        name = str(art.get("name") or "").strip()
        if not name:
            continue
        name_l = name.lower()
        mentions = [ln for ln in text.splitlines() if name_l in ln.lower()]
        if any(SHARING_NOW_RE.search(ln) for ln in mentions) and any(
            SHARING_LATER_RE.search(ln) for ln in mentions
        ):
            reasons.append(f"Artifact dual-share contradiction: {name}")
        state = str(art.get("state") or "")
        if state == "referenced_only" and name_l and name_l in sharing_block.lower():
            if SHARING_SECTION_RE.search(sharing_block):
                reasons.append(f"Artifact with state referenced_only appears in sharing section: {name}")

    client_bullets = _section_bullets(text, CLIENT_ACTIONS_HEADER_RE)
    our_bullets = _section_bullets(text, OUR_ACTIONS_HEADER_RE)
    client_stems = {_action_stem(b) for b in client_bullets if _action_stem(b)}
    for b in our_bullets:
        stem = _action_stem(b)
        if stem and stem in client_stems:
            reasons.append(f"Same action stem in both client and our-side sections: {stem}")
            break

    for b in our_bullets:
        # Strip leading "- " already done; check imperative after optional article.
        lead = re.sub(r"^(?:the|a|an)\s+", "", b.strip(), flags=re.I)
        if IMPERATIVE_STEM_RE.match(lead):
            reasons.append(f"Our-side bullet starts with an imperative verb: {b[:60]}")
            break

    for section_bullets in (our_bullets, client_bullets):
        head_counts: dict[str, int] = {}
        for b in section_bullets:
            hn = _head_noun(b)
            if hn:
                head_counts[hn] = head_counts.get(hn, 0) + 1
        for hn, n in head_counts.items():
            if n > 1:
                reasons.append(f"Duplicate head noun across bullets: {hn}")
                break
        else:
            continue
        break

    recap = _section_bullets(text, RECAP_HEADER_RE)
    if len(recap) > 6:
        reasons.append(f"Recap bullet count > 6 ({len(recap)})")

    decisions = list(extraction.get("decisions") or [])
    for bullet in recap:
        bnorm = _normalize(bullet)
        best = None
        best_score = 0.0
        for d in decisions:
            dtext = _normalize(str(d.get("text") or ""))
            if not dtext:
                continue
            score = 1.0 if dtext in bnorm or bnorm in dtext else dice_coefficient(dtext, bnorm)
            if score > best_score:
                best_score = score
                best = d
        if best is None or best_score < 0.62:
            continue
        impact = str(best.get("impact") or "")
        allowed = (
            impact in {"project", "multi_template"}
            or bool(best.get("reverses_prior_assumption"))
            or bool(best.get("is_rejection"))
        )
        if not allowed:
            reasons.append(f"Recap includes low-impact decision: {best.get('text')}")

    # Timing: if a bullet carries a timing cue, it must match that item's timing_verbatim.
    timed_items: list[tuple[str, str]] = []
    for commit in extraction.get("our_commitments") or []:
        timing = str(commit.get("timing_verbatim") or "").strip()
        if timing:
            timed_items.append((_normalize(str(commit.get("text") or "")), timing))
    for action in extraction.get("client_actions") or []:
        timing = str(action.get("timing_verbatim") or "").strip()
        if timing:
            timed_items.append((_normalize(str(action.get("text") or "")), timing))

    for bullet in our_bullets + client_bullets:
        bnorm = _normalize(bullet)
        bullet_signals = timing_signals(bullet) if _TIMING_PHRASE_RE.search(bullet) else []
        if not bullet_signals:
            continue
        matched_item = False
        for ctext, timing in timed_items:
            if not ctext:
                continue
            if ctext in bnorm or bnorm in ctext or dice_coefficient(ctext, bnorm) >= 0.55:
                if timing_present_in_body(timing, bullet) or any(
                    sig in _normalize(timing) for sig in bullet_signals
                ):
                    matched_item = True
                    break
        if not matched_item:
            # Soft: only fail when the suffix looks like a known timing phrase with no home.
            reasons.append(
                f"Timing suffix not traceable to that item's timing_verbatim: {bullet_signals[0]}"
            )

    for commit in extraction.get("our_commitments") or []:
        timing = str(commit.get("timing_verbatim") or "").strip()
        if not timing:
            continue
        if not timing_present_in_body(timing, text):
            reasons.append(
                f"Commitment timing missing from body: {', '.join(timing_signals(timing)) or timing[:60]}"
            )

    urgent_present = False
    for flag in extraction.get("flags") or []:
        if str(flag.get("severity") or "").lower() != "urgent":
            continue
        urgent_present = True
        ftext = str(flag.get("text") or "").strip()
        if ftext and _normalize(ftext) in _normalize(text):
            reasons.append("Urgent flag text appears in body")

    if urgent_present:
        sep_ok = (
            isinstance(separate_email_recommended, dict)
            and bool(separate_email_recommended.get("subject") or separate_email_recommended.get("why"))
        )
        if not sep_ok:
            reasons.append("Urgent flag present but separate_email_recommended is null")

    # Never end on a bullet list (closer / sign-off must follow).
    nonempty = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(nonempty) >= 3:
        # Last line = sign-off name; second-to-last often "Best regards,"; check third-to-last.
        candidate = nonempty[-3] if nonempty[-2].lower().startswith("best regard") else nonempty[-2]
        if candidate.startswith("- ") or candidate.startswith("* ") or candidate.startswith("-"):
            reasons.append("Email ends on a bullet")

    return (len(reasons) == 0), reasons


def repair_composition_body(*, body: str, extraction: dict) -> str:
    """Best-effort deterministic fixes for soft Step-4 failures (timing / weak recap)."""
    text = body or ""
    # Append missing commitment timing suffixes on matching our-actions bullets.
    our_bullets = _section_bullets(text, OUR_ACTIONS_HEADER_RE)
    if our_bullets:
        lines = text.splitlines()
        in_our = False
        out_lines: list[str] = []
        for line in lines:
            if OUR_ACTIONS_HEADER_RE.search(line):
                in_our = True
                out_lines.append(line)
                continue
            if in_our:
                s = line.strip()
                if not s:
                    in_our = False
                    out_lines.append(line)
                    continue
                if s.startswith("- ") or s.startswith("* "):
                    bullet = s[2:].strip()
                    bnorm = _normalize(bullet)
                    for commit in extraction.get("our_commitments") or []:
                        ctext = _normalize(str(commit.get("text") or ""))
                        timing = str(commit.get("timing_verbatim") or "").strip()
                        if not ctext or not timing:
                            continue
                        if ctext in bnorm or bnorm in ctext or dice_coefficient(ctext, bnorm) >= 0.55:
                            if timing_present_in_body(timing, bullet):
                                break
                            signals = timing_signals(timing)
                            suffix = signals[0] if signals else None
                            if suffix and suffix not in bnorm:
                                prefix = "- " if s.startswith("- ") else "* "
                                line = f"{prefix}{bullet} - {suffix}"
                            break
                    out_lines.append(line)
                    continue
                in_our = False
            out_lines.append(line)
        text = "\n".join(out_lines)

    # Drop recap bullets whose best-matching decision is low-impact.
    recap = _section_bullets(text, RECAP_HEADER_RE)
    if recap:
        decisions = list(extraction.get("decisions") or [])
        drop: set[str] = set()
        for bullet in recap:
            bnorm = _normalize(bullet)
            best = None
            best_score = 0.0
            for d in decisions:
                dtext = _normalize(str(d.get("text") or ""))
                if not dtext:
                    continue
                score = 1.0 if dtext in bnorm or bnorm in dtext else dice_coefficient(dtext, bnorm)
                if score > best_score:
                    best_score = score
                    best = d
            if (
                best
                and best_score >= 0.62
                and str(best.get("impact") or "") in {"single_screen", "detail"}
                and not best.get("reverses_prior_assumption")
                and not best.get("is_rejection")
            ):
                drop.add(_normalize(bullet))
        if drop:
            lines = text.splitlines()
            in_recap = False
            kept_recap = 0
            out_lines = []
            for line in lines:
                if RECAP_HEADER_RE.search(line):
                    in_recap = True
                    out_lines.append(line)
                    continue
                if in_recap:
                    s = line.strip()
                    if not s:
                        if kept_recap == 0:
                            # Remove empty recap header we just added if all bullets dropped
                            if out_lines and RECAP_HEADER_RE.search(out_lines[-1]):
                                out_lines.pop()
                        in_recap = False
                        out_lines.append(line)
                        continue
                    if s.startswith("- ") or s.startswith("* "):
                        bullet = s[2:].strip()
                        if _normalize(bullet) in drop:
                            continue
                        kept_recap += 1
                        out_lines.append(line)
                        continue
                    in_recap = False
                out_lines.append(line)
            text = "\n".join(out_lines)

    # Strip em dashes to house-style hyphens.
    text = EM_DASH_RE.sub(" - ", text)
    return text


def build_review_table(*, body: str, extraction: dict) -> list[dict]:
    """Map each body bullet to a fact-sheet item + evidence for human review."""
    candidates: list[dict] = []
    for d in extraction.get("decisions") or []:
        candidates.append(
            {
                "source": "decisions",
                "text": str(d.get("text") or ""),
                "impact": d.get("impact"),
                "evidence": d.get("evidence") or "",
                "reverses_prior_assumption": bool(d.get("reverses_prior_assumption")),
                "is_rejection": bool(d.get("is_rejection")),
                "owner": None,
            }
        )
    for c in extraction.get("our_commitments") or []:
        candidates.append(
            {
                "source": "our_commitments",
                "text": str(c.get("text") or ""),
                "impact": None,
                "evidence": c.get("evidence") or "",
                "timing_verbatim": c.get("timing_verbatim"),
            }
        )
    for c in extraction.get("client_actions") or []:
        candidates.append(
            {
                "source": "client_actions",
                "text": str(c.get("text") or ""),
                "impact": None,
                "evidence": c.get("evidence") or "",
                "blocking": c.get("blocking"),
                "owner": c.get("owner"),
            }
        )
    for q in extraction.get("open_questions") or []:
        candidates.append(
            {
                "source": "open_questions",
                "text": str(q.get("question") or ""),
                "impact": None,
                "evidence": q.get("evidence") or "",
                "answer_owner": q.get("answer_owner"),
                "missing_parameter": q.get("missing_parameter"),
            }
        )
    for a in extraction.get("artifacts") or []:
        candidates.append(
            {
                "source": "artifacts",
                "text": str(a.get("name") or ""),
                "impact": None,
                "evidence": a.get("evidence") or "",
                "state": a.get("state"),
            }
        )

    rows: list[dict] = []
    for bullet in _body_bullets(body):
        bnorm = _normalize(bullet)
        best = None
        best_score = 0.0
        for cand in candidates:
            ctext = _normalize(str(cand.get("text") or ""))
            if not ctext:
                continue
            score = 1.0 if ctext in bnorm or bnorm in ctext else dice_coefficient(ctext, bnorm)
            if score > best_score:
                best_score = score
                best = cand
        rows.append(
            {
                "bullet": bullet,
                "matched": best_score >= 0.45,
                "score": round(best_score, 3),
                "source": (best or {}).get("source"),
                "fact": (best or {}).get("text") if best_score >= 0.45 else None,
                "impact": (best or {}).get("impact") if best_score >= 0.45 else None,
                "owner": (
                    ((best or {}).get("owner") or (best or {}).get("answer_owner"))
                    if best_score >= 0.45
                    else None
                ),
                "evidence": (best or {}).get("evidence") if best_score >= 0.45 else None,
            }
        )
    return rows


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


ALLOWED_PLACEHOLDER_RE = re.compile(
    r"\[(Figma link|link|date|name|option 1|option 2)\]",
    re.I,
)
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
    return [
        p
        for p in ANY_BRACKET_RE.findall(body or "")
        if p[1:-1].strip().lower() not in ALLOWED_BRACKET_TOKENS
    ]



DEGRADED_DRAFT_REASON = "Degraded transcript — draft must be reviewed before sending"


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
    if transcript_quality == "degraded":
        reasons.append(DEGRADED_DRAFT_REASON)
    return (len(reasons) == 0, reasons)


def _only_degraded_quality(reasons: list[str]) -> bool:
    """True when the only guard failure is the degraded-transcript review gate."""
    hits = [r for r in reasons if r.strip()]
    return bool(hits) and all("degraded transcript" in r.lower() for r in hits)


def skeleton_composition(
    *,
    project_name: str,
    date_label: str,
    owner_name: str,
    client_names: str = "[name]",
    thanks_line: str = "Thank you for the call!",
    reviewer_notes: list[str] | None = None,
) -> dict[str, Any]:
    _ = date_label  # kept for call-site compatibility; subject no longer embeds the date
    sign_off = sender_first_name(owner_name)
    subject = f"{project_name} - follow-up from our call"
    body = "\n".join(
        [
            f"Hello {client_names or '[name]'},",
            "",
            thanks_line,
            "",
            "As next steps from your side, please:",
            "- Follow-up details to be confirmed ([date])",
            "",
            "We will come back to you once details are confirmed.",
            "",
            "Best regards,",
            sign_off,
        ]
    )
    return {
        "subject": subject,
        "body": body,
        "reviewer_notes": list(reviewer_notes or [])
        + ["Skeleton draft: fact sheet was empty or policy blocked regeneration."],
        "separate_email_recommended": None,
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


def client_display_names(
    attendees: list[dict],
    client_participants: list[str] | None,
    *,
    unresolved: list[str] | None = None,
) -> str:
    """Full client display names from attendee name fields only (never email local-parts)."""
    client_set = {e.lower() for e in (client_participants or [])}
    names: list[str] = []
    for a in attendees:
        email = (a.get("email") or "").lower()
        if not email:
            continue
        is_client = email in client_set if client_set else not is_internal_email(email)
        if not is_client:
            continue
        raw_name = (a.get("name") or a.get("resolvedName") or "").strip()
        token = first_token(raw_name)
        if token:
            # Prefer full display name when it looks human; otherwise first token.
            if "@" in raw_name:
                names.append(token)
            else:
                names.append(raw_name)
        else:
            names.append("[name]")
            if unresolved is not None:
                unresolved.append(email or raw_name or "(unknown)")
    return ", ".join(names)


def client_first_names(
    attendees: list[dict],
    client_participants: list[str] | None,
    *,
    unresolved: list[str] | None = None,
) -> str:
    """First names only for the greeting line — transcript attendee names, never emails."""
    client_set = {e.lower() for e in (client_participants or [])}
    parts: list[str] = []
    for a in attendees:
        email = (a.get("email") or "").lower()
        if not email:
            continue
        is_client = email in client_set if client_set else not is_internal_email(email)
        if not is_client:
            continue
        raw_name = (a.get("name") or a.get("resolvedName") or "").strip()
        token = first_token(raw_name)
        if token:
            parts.append(token)
        else:
            parts.append("[name]")
            if unresolved is not None:
                unresolved.append(email or raw_name or "(unknown)")
    return ", ".join(parts) if parts else "[name]"


def _humanize_validator_reason(reason: str) -> dict[str, str]:
    """Turn a raw validator string into a short card: label / detail / fix."""
    r = (reason or "").strip()
    empty = {"label": "Check failed", "detail": r or "Unknown issue.", "fix": "Review the email and regenerate if needed.", "tag": "other"}
    if not r:
        return empty
    low = r.lower()
    detail = r.split(":", 1)[-1].strip() if ":" in r else ""

    if "commitment timing missing" in low:
        return {
            "label": "Missing delivery timing",
            "detail": (
                f"A commitment from the call had timing (“{detail}”), but that timing "
                "didn’t make it into the email body."
                if detail
                else "A commitment’s timing from the call is missing in the email body."
            ),
            "fix": "Add the timing to the matching “From our side” bullet (e.g. “ - next week”), then send.",
            "tag": "timing",
        }
    if "recap includes low-impact" in low:
        return {
            "label": "Too-small item in the recap",
            "detail": (
                f"The alignment section included a small UI/detail change that belongs in the designs, not the email"
                + (f": “{detail}”." if detail else ".")
            ),
            "fix": "Delete that bullet from the recap (or regenerate). Keep only big project decisions.",
            "tag": "recap",
        }
    if "recap bullet count" in low:
        n = ""
        m = re.search(r"\((\d+)\)", r)
        if m:
            n = m.group(1)
        return {
            "label": "Recap is too long",
            "detail": (
                f"The alignment section has {n} bullets; the house format allows at most 6."
                if n
                else "The alignment section has more than 6 bullets."
            ),
            "fix": "Keep the 6 most important project/multi-template points (prefer reversals/rejections), drop the rest.",
            "tag": "recap",
        }
    if "sign-off is not" in low:
        return {
            "label": "Sign-off should be first name only",
            "detail": "The email should end with the sender’s first name, not a full name.",
            "fix": "Replace the last line with the account owner’s first name.",
            "tag": "style",
        }
    if "email-derived" in low:
        return {
            "label": "Greeting looks like an email address",
            "detail": "Client names in the greeting came from email local-parts instead of real names.",
            "fix": "Replace with first names from the calendar/transcript attendee list (or [name]).",
            "tag": "style",
        }
    if "call-time deictic" in low:
        return {
            "label": "Call-time wording",
            "detail": "The draft still sounds like it was written during the call (“after the call”, “on my screen”, …).",
            "fix": "Rewrite for a reader opening this after the meeting (e.g. “today”, “on the call”).",
            "tag": "style",
        }
    if "as promised" in low:
        return {
            "label": "“As promised” used more than once",
            "detail": "House format allows “As promised” at most once in the whole email.",
            "fix": "Keep one sharing lead-in; drop the extra “As promised”.",
            "tag": "style",
        }
    if "imperative verb" in low:
        return {
            "label": "Our-side bullet starts with a command",
            "detail": "“From our side we will proceed with:” needs a gerund or noun phrase, not “Send …” / “Deliver …”.",
            "fix": "Rewrite as “Sending …”, “Delivering …”, or a noun phrase.",
            "tag": "style",
        }
    if "same action stem" in low:
        return {
            "label": "Same action on both sides",
            "detail": "The same to-do appears under both client and our-side lists.",
            "fix": "Keep it on the side that will actually perform the work.",
            "tag": "ownership",
        }
    if "separate_email_recommended is null" in low:
        return {
            "label": "Urgent flag not routed",
            "detail": "An urgent security/legal item was found but no separate-email recommendation was produced.",
            "fix": "Remove it from the follow-up and send a short separate note.",
            "tag": "risk",
        }
    if "ends on a bullet" in low:
        return {
            "label": "Email ends on a bullet list",
            "detail": "There should be a closer line (next session / we will come back) before the sign-off.",
            "fix": "Add a one-line closer, then Best regards + first name.",
            "tag": "structure",
        }
    if "em dash" in low:
        return {
            "label": "Wrong dash character",
            "detail": "The draft used an em dash (—). Client emails use a plain hyphen with spaces ( - ).",
            "fix": "Replace — with “ - ” before sending.",
            "tag": "style",
        }
    if "banned filler" in low or "banned phrase" in low:
        return {
            "label": "Corporate filler language",
            "detail": "The draft included phrases we avoid (e.g. “circling back”, “hope this finds you well”).",
            "fix": "Remove the filler and keep the email warm but direct.",
            "tag": "style",
        }
    if "disallowed bracket" in low or "disallowed placeholder" in low:
        return {
            "label": "Invalid placeholder",
            "detail": (
                f"The draft has a bracket token that isn’t allowed for a sendable email"
                + (f" ({detail})." if detail else ".")
            ),
            "fix": "Only use [Figma link], [link], [date], [name], [option 1], or [option 2] — or fill the real value.",
            "tag": "placeholder",
        }
    if "dual-share" in low or "artifact dual" in low:
        return {
            "label": "Conflicting share status",
            "detail": (
                "The same design file was described as both already shared and still being adjusted."
                + (f" ({detail})" if detail else "")
            ),
            "fix": "Pick one: either share the link now, or say you’ll send after changes — not both.",
            "tag": "artifact",
        }
    if "urgent flag" in low:
        return {
            "label": "Urgent item in the follow-up",
            "detail": "A security/legal/risk item was written into the normal follow-up body.",
            "fix": "Remove it from this email and send a separate short note about it.",
            "tag": "risk",
        }
    if "currency" in low:
        return {
            "label": "Unverified price",
            "detail": "A currency/amount appeared that wasn’t found in the call source.",
            "fix": "Remove the number unless you can confirm it from the transcript or notes.",
            "tag": "safety",
        }
    if "effort estimate" in low:
        return {
            "label": "Unverified effort estimate",
            "detail": "Hours/days appeared that weren’t found in the call source.",
            "fix": "Remove the estimate unless it was explicitly said on the call.",
            "tag": "safety",
        }
    if "scope-expansion" in low:
        return {
            "label": "Sounds like new scope",
            "detail": "Wording looked like a new commitment rather than confirming what was already agreed.",
            "fix": "Rephrase as confirmation of the call alignment, not a new promise.",
            "tag": "safety",
        }
    if "url not on allowlist" in low:
        return {
            "label": "Unexpected link",
            "detail": "The draft included a URL that wasn’t in the transcript or project link registry.",
            "fix": "Replace with a known Figma/Notion link, or use [Figma link] / [link].",
            "tag": "links",
        }
    if "degraded transcript" in low:
        return {
            "label": "Messy transcript, sendable-looking draft",
            "detail": (
                "Auto-transcript quality was weak, but the email still reads as ready to send. "
                "That is easy to over-trust."
            ),
            "fix": "Verify every bullet against your own notes before sending.",
            "tag": "quality",
        }
    if "timed out" in low:
        return {
            "label": "Generation timed out",
            "detail": "The draft job ran too long and was stopped.",
            "fix": "Click Regenerate and wait for it to finish.",
            "tag": "system",
        }
    if "interrupted" in low:
        return {
            "label": "Generation interrupted",
            "detail": "The server restarted while this draft was generating.",
            "fix": "Click Regenerate.",
            "tag": "system",
        }
    return {
        "label": "Automatic check flagged this",
        "detail": r,
        "fix": "Review the email carefully, edit if needed, or regenerate.",
        "tag": "other",
    }


def _split_policy_reasons(reason: str | None) -> list[str]:
    raw = (reason or "").strip()
    if not raw:
        return []
    # Reasons are joined with "; " from the guard list.
    parts = [p.strip() for p in re.split(r";\s*", raw) if p.strip()]
    return parts or [raw]


def _is_skeleton_body(body: str | None) -> bool:
    text = body or ""
    return "Follow-up details to be confirmed" in text


def email_bodies_equivalent(left: str | None, right: str | None) -> bool:
    """True when two email bodies are the same for display purposes."""

    def _norm(value: str | None) -> str:
        text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        lines = [" ".join(line.split()) for line in text.split("\n")]
        return "\n".join(line for line in lines if line).strip()

    return _norm(left) == _norm(right)


def explain_draft_status(
    *,
    body_text: str | None,
    policy_blocked: bool,
    policy_block_reason: str | None,
    reviewer_notes: list | None,
    transcript_quality: str | None,
    low_confidence: bool,
    placeholder_count: int,
    separate_email: dict | None = None,
    extraction: dict | None = None,
) -> dict[str, Any]:
    """User-facing summary of what happened during draft generation."""
    notes = [str(n).strip() for n in (reviewer_notes or []) if str(n).strip()]
    skeleton = _is_skeleton_body(body_text) or any("skeleton draft" in n.lower() for n in notes)
    kept_despite_validators = any(
        "validators still failing after retry" in n.lower()
        or "draft kept" in n.lower()
        or "cleaned follow-up was prepared" in n.lower()
        or "kept raw model" in n.lower()
        for n in notes
    )
    issues = [_humanize_validator_reason(r) for r in _split_policy_reasons(policy_block_reason)]
    issues = [i for i in issues if i.get("detail")]
    reasons = [i["detail"] for i in issues]  # plain strings for older callers/tests

    # Categorize remaining notes for the details panel.
    categories: dict[str, list[str]] = {
        "blocking": [],
        "quality": [],
        "critic": [],
        "links": [],
        "other": [],
    }
    for n in notes:
        low = n.lower()
        if (
            "policy/composition" in low
            or "validators still failing" in low
            or "guard blocked" in low
            or "skeleton draft" in low
        ):
            categories["blocking"].append(n)
        elif "transcript quality" in low or "low-confidence" in low or "low confidence" in low:
            categories["quality"].append(n)
        elif low.startswith("critic ") or "critic " in low[:20]:
            categories["critic"].append(n)
        elif "jira project" in low or "figma" in low or "link lookup" in low or "notion" in low:
            categories["links"].append(n)
        else:
            categories["other"].append(n)

    facts = extraction if isinstance(extraction, dict) else {}
    fact_counts = {
        "decisions": len(facts.get("decisions") or []),
        "commitments": len(facts.get("our_commitments") or []),
        "client_actions": len(facts.get("client_actions") or []),
        "open_questions": len(facts.get("open_questions") or []),
        "flags": len(facts.get("flags") or []),
    }

    if skeleton and (policy_blocked or issues):
        level = "error"
        title = "No sendable follow-up yet"
        summary = (
            "Automatic checks blocked this run. Only a short placeholder was stored. "
            "Regenerate to get a full client email."
        )
        list_label = "placeholder"
        next_step = "Click Regenerate, then review the new follow-up."
        show_primary_email = False
        show_kept_email = True
    elif policy_blocked and kept_despite_validators:
        level = "warn"
        title = "Almost ready — fix these before sending"
        summary = (
            "A follow-up email was generated (shown below). "
            "A few house-style checks still need your eye."
        )
        list_label = "needs review"
        next_step = "Skim the issues, edit the follow-up if needed, then copy to send."
        show_primary_email = True
        show_kept_email = False
    elif policy_blocked:
        level = "warn"
        title = "Almost ready — fix these before sending"
        summary = (
            "A follow-up email was generated (shown below). "
            "Automatic checks flagged a few things to confirm."
        )
        list_label = "needs review"
        next_step = "Resolve the issues below, then copy subject and body."
        show_primary_email = not skeleton
        show_kept_email = False
    elif low_confidence or (transcript_quality or "").lower() == "degraded":
        level = "warn"
        title = "Transcript looked messy"
        summary = (
            "Auto-transcript quality was weak, so treat this draft as a starting point "
            "and verify against your own notes."
        )
        list_label = "check transcript"
        next_step = "Compare key bullets with the call before sending."
        show_primary_email = True
        show_kept_email = False
    elif separate_email and (separate_email.get("subject") or separate_email.get("why")):
        level = "warn"
        title = "Follow-up ready — plus a separate email"
        summary = (
            "The client follow-up looks usable, but an urgent item should go in its own email."
        )
        list_label = "separate email"
        next_step = "Send the follow-up, then send the separate urgent note."
        show_primary_email = True
        show_kept_email = False
    elif placeholder_count:
        level = "info"
        title = "Draft ready — placeholders to fill"
        summary = (
            f"The email is mostly ready, with {placeholder_count} placeholder"
            f"{'' if placeholder_count == 1 else 's'} (e.g. [Figma link] or [date]) to fill in."
        )
        list_label = "fill placeholders"
        next_step = "Replace bracket placeholders, then copy to send."
        show_primary_email = True
        show_kept_email = False
    else:
        level = "ok"
        title = "Draft ready to review"
        summary = "Generation finished without blocking issues. Give it a quick human pass, then send."
        list_label = "ready"
        next_step = "Copy subject and body into your email client."
        show_primary_email = True
        show_kept_email = False

    return {
        "level": level,  # ok | info | warn | error
        "title": title,
        "summary": summary,
        "list_label": list_label,
        "next_step": next_step,
        "reasons": reasons,
        "issues": issues,
        "issue_count": len(issues),
        "skeleton": skeleton,
        "fact_counts": fact_counts,
        "categories": categories,
        "has_details": any(categories.values()) or bool(issues) or bool(separate_email),
        "show_primary_email": show_primary_email,
        "show_kept_email": show_kept_email,
    }


def _empty_extraction() -> dict:
    return {
        "meeting": {
            "title": "",
            "date_iso": None,
            "attendees_client": [],
            "attendees_internal": [],
        },
        "transcript_quality": "degraded",
        "decisions": [],
        "our_commitments": [],
        "client_actions": [],
        "open_questions": [],
        "flags": [],
        "artifacts": [],
        "next_meeting": {"date_iso": None, "timing_verbatim": None, "evidence": ""},
        "topics_discussed_unclear_outcome": [],
    }


def _fact_sheet_empty(extraction: dict) -> bool:
    return not any(
        [
            extraction.get("decisions"),
            extraction.get("our_commitments"),
            extraction.get("client_actions"),
            extraction.get("open_questions"),
            extraction.get("flags"),
            extraction.get("artifacts"),
            extraction.get("topics_discussed_unclear_outcome"),
            (extraction.get("next_meeting") or {}).get("date_iso"),
            (extraction.get("next_meeting") or {}).get("timing_verbatim"),
        ]
    )


def _llm_json(
    system: str,
    user: str,
    client: LLMClient,
    *,
    model: str | None = None,
    max_tokens: int = 8000,
) -> tuple[dict, LLMResult]:
    result = client.synthesize(
        system=system, user_content=user, max_tokens=max_tokens, model=model
    )
    try:
        return parse_digest_json(result.text), result
    except ValueError as e:
        raise RuntimeError(f"LLM returned unparseable JSON: {e}") from e


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
    name_notes: list[str] = []
    unresolved_client: list[str] = []
    client_names = client_first_names(
        attendees, client_participants, unresolved=unresolved_client
    )
    for email in unresolved_client:
        name_notes.append(
            f"Could not resolve client first name from transcript attendees for {email}; used [name]."
        )
    sender_name = sender_first_name(owner_name)
    from datetime import datetime as _dt

    try:
        thanks_line = compute_thanks_line(
            call_datetime=item.get("eventStartTime"),
            send_datetime=_dt.now().astimezone(),
        )
    except ValueError as e:
        thanks_line = "Thank you for the call!"
        name_notes.append(f"Thanks-line date delta invalid ({e}); used generic thanks.")

    client = llm or LLMClient()
    critic_model = getattr(client.s, "call_summary_critic_model", None) or "claude-haiku-4-5-20251001"
    total_in = total_out = 0
    total_cost = 0.0
    critic_notes: list[str] = list(name_notes)

    prompts = resolve_system_prompts(session)
    extraction_system = prompts["extraction"]
    critic_system = prompts["critic"]
    composition_system = prompts["composition"]

    skill_extra = ""
    if SKILL_PATH.is_file():
        skill_extra = "\n\n" + SKILL_PATH.read_text(encoding="utf-8")[:4000]

    low_note = ""
    if quality.low_confidence:
        low_note = (
            "\n\nQUALITY GATE: low-confidence transcript. Be maximally conservative; "
            'prefer topics_discussed_unclear_outcome; set transcript_quality to "degraded". '
            "Still apply the reversal sweep and impact classification."
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
        extraction, r1 = _llm_json(extraction_system + skill_extra, user_extract, client)
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

    # Critic pass (v3 Step 2) — cheaper model; skip when extraction already empty
    if not _fact_sheet_empty(extraction) or content.strip():
        user_critic = (
            f"Transcript:\n{content}\n\nFact sheet JSON:\n{json.dumps(extraction, indent=2)}"
        )
        try:
            critic, rc = _llm_json(
                critic_system,
                user_critic,
                client,
                model=critic_model,
                max_tokens=4000,
            )
            total_in += rc.input_tokens
            total_out += rc.output_tokens
            total_cost += rc.cost_usd
            extraction, critic_merge_notes = merge_critic_into_extraction(extraction, critic, content)
            critic_notes.extend(critic_merge_notes)
            extraction, dropped2 = validate_extraction_evidence(extraction, content)
            if dropped2:
                log.info(
                    "Dropped %s critic-added items with unmatched evidence (%s)",
                    dropped2,
                    transcript_id,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("Critic pass failed (continuing with extraction): %s", e)
            critic_notes.append(f"Critic pass failed: {e}")

    explicit_urls = extract_explicit_urls(content)
    arts = []
    for art in extraction.get("artifacts") or []:
        url = art.get("explicit_url")
        if url and url not in explicit_urls and str(url) not in content:
            arts.append({**art, "explicit_url": None})
        else:
            arts.append(art)
    extraction["artifacts"] = arts
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
    fact_empty = _fact_sheet_empty(extraction)
    meeting_date_label = meeting_date or str(
        (extraction.get("meeting") or {}).get("date_iso") or "[date]"
    )

    def _composition_user(*, extra: str = "") -> str:
        return (
            f"THANKS_LINE: {thanks_line}\n"
            f"CLIENT_FIRST_NAMES: {client_names or '[name]'}\n"
            f"SENDER_FIRST_NAME: {sender_name}\n"
            f"PROJECT_NAME: {project_name}\n"
            f"Fact sheet:\n{json.dumps(extraction, indent=2)}\n"
            f"Resolved links:\n{json.dumps(link_map, indent=2)}"
            + (f"\n{extra}" if extra else "")
        )

    if fact_empty:
        composition = skeleton_composition(
            project_name=str(project_name),
            date_label=meeting_date_label,
            owner_name=owner_name,
            client_names=client_names,
            thanks_line=thanks_line,
            reviewer_notes=["Empty fact sheet after extraction/validation — skeleton draft only."],
        )
    else:
        try:
            composition, r2 = _llm_json(composition_system, _composition_user(), client)
            total_in += r2.input_tokens
            total_out += r2.output_tokens
            total_cost += r2.cost_usd
            composition.setdefault("reviewer_notes", [])
            composition.setdefault("separate_email_recommended", None)
        except Exception as e:  # noqa: BLE001
            log.exception("Composition failed: %s", e)
            composition = skeleton_composition(
                project_name=str(project_name),
                date_label=meeting_date_label,
                owner_name=owner_name,
                client_names=client_names,
                thanks_line=thanks_line,
                reviewer_notes=[f"Composition LLM failed: {e}"],
            )

    source_for_policy = json.dumps(extraction) + "\n" + content
    tq = str(extraction.get("transcript_quality") or "good")

    def _guards(body: str, composition_obj: dict | None = None) -> tuple[bool, list[str]]:
        sep = None
        if composition_obj and isinstance(composition_obj.get("separate_email_recommended"), dict):
            sep = composition_obj.get("separate_email_recommended")
        ok_p, reasons_p = run_policy_guard(
            body=body,
            transcript_quality=tq,
            source_text=source_for_policy,
            allowed_urls=allowed_urls,
        )
        ok_c, reasons_c = validate_composition_draft(
            body=body,
            extraction=extraction,
            sender_first_name=sender_name,
            separate_email_recommended=sep if isinstance(sep, dict) else None,
        )
        return (ok_p and ok_c), reasons_p + reasons_c

    ok, reasons = _guards(str(composition.get("body") or ""), composition)

    # Deterministic fill: urgent flags must route to a separate email recommendation.
    urgent_flags = [
        f
        for f in (extraction.get("flags") or [])
        if str(f.get("severity") or "").lower() == "urgent"
    ]
    if urgent_flags:
        sep = composition.get("separate_email_recommended")
        if not (isinstance(sep, dict) and (sep.get("subject") or sep.get("why"))):
            first = str(urgent_flags[0].get("text") or "urgent risk item").strip()
            composition["separate_email_recommended"] = {
                "subject": f"{project_name} - urgent follow-up",
                "why": first[:200],
            }
            notes = list(composition.get("reviewer_notes") or [])
            notes.insert(
                0,
                "Urgent flag present — separate_email_recommended was empty; filled a placeholder subject/why for review.",
            )
            composition["reviewer_notes"] = notes
            ok, reasons = _guards(str(composition.get("body") or ""), composition)

    policy_blocked = False
    policy_block_reason: str | None = None
    kept_body_raw: str | None = None

    # Degraded quality cannot be fixed by rewriting the email — skip the extra LLM retry.
    if not ok and not fact_empty and not _only_degraded_quality(reasons):
        log.warning("Composition validators failed (%s); regenerating once", reasons)
        try:
            composition, r3 = _llm_json(
                composition_system,
                _composition_user(
                    extra=f"Previous draft failed validation: {'; '.join(reasons)}. Fix and regenerate."
                ),
                client,
            )
            total_in += r3.input_tokens
            total_out += r3.output_tokens
            total_cost += r3.cost_usd
            composition.setdefault("reviewer_notes", [])
            composition.setdefault("separate_email_recommended", None)
        except Exception:
            pass
        ok, reasons = _guards(str(composition.get("body") or ""), composition)

    if not ok and not fact_empty and _only_degraded_quality(reasons):
        policy_blocked = True
        policy_block_reason = "; ".join(reasons)
        log.warning("Degraded transcript; blocking send-as-is (%s)", policy_block_reason)
    elif not ok and not fact_empty and not _is_skeleton_body(str(composition.get("body") or "")):
        # Keep the model output as-is, then produce a cleaned "as it should be" body.
        kept_body_raw = str(composition.get("body") or "")
        repaired = repair_composition_body(body=kept_body_raw, extraction=extraction)
        ok_r, reasons_r = _guards(repaired, composition)
        composition["body"] = repaired
        policy_blocked = True
        policy_block_reason = "; ".join(reasons_r if not ok_r else reasons)
        notes = list(composition.get("reviewer_notes") or [])
        if ok_r:
            notes.insert(
                0,
                "Validators failed on the raw model email; a cleaned follow-up was prepared. "
                "Original model output is kept below for comparison.",
            )
            policy_block_reason = "; ".join(reasons)  # original failure reasons for UI
        else:
            notes.insert(
                0,
                "Composition validators still failing after retry+repair (cleaned draft kept): "
                f"{policy_block_reason}",
            )
        composition["reviewer_notes"] = notes
        ok = ok_r
        log.warning(
            "Composition validators failed; kept raw model body and prepared cleaned draft (%s)",
            policy_block_reason,
        )
    elif not ok:
        # Skeleton / empty-fact path — nothing better to keep.
        policy_blocked = True
        policy_block_reason = "; ".join(reasons)
        notes = list(composition.get("reviewer_notes") or [])
        notes.insert(
            0,
            f"Composition validators failed (placeholder draft): {policy_block_reason}",
        )
        composition["reviewer_notes"] = notes
        kept_body_raw = str(composition.get("body") or "")
        log.warning(
            "Composition validators failed on placeholder/empty path (%s)",
            policy_block_reason,
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
    kept_body_final: str | None = None
    if kept_body_raw is not None:
        kept_body_final = inject_resolved_urls_into_body(kept_body_raw, link_map, arts)
        if email_bodies_equivalent(kept_body_final, body_final):
            kept_body_final = None

    reviewer_notes = list(composition.get("reviewer_notes") or [])
    for n in critic_notes:
        if n and n not in reviewer_notes:
            reviewer_notes.append(n)
    for n in link_notes:
        if n and n not in reviewer_notes:
            reviewer_notes.append(n)
    if call_jira_key:
        scope_note = f"Artifact links looked up in Jira project {call_jira_key} only (from call title)."
        if scope_note not in reviewer_notes:
            reviewer_notes.insert(0, scope_note)

    separate = composition.get("separate_email_recommended")
    if isinstance(separate, dict) and (separate.get("subject") or separate.get("why")):
        sep_note = (
            "SEPARATE EMAIL RECOMMENDED: "
            f"subject={separate.get('subject') or '(none)'} — "
            f"{separate.get('why') or ''}"
        )
        if sep_note not in reviewer_notes:
            reviewer_notes.insert(0, sep_note)

    composition["reviewer_notes"] = reviewer_notes
    review_table = build_review_table(body=body_final, extraction=extraction)

    placeholder_count = count_placeholders(str(composition.get("body") or ""))
    reviewer_notes = list(composition.get("reviewer_notes") or [])

    # Persist pipeline extras alongside the fact sheet (no schema migration).
    extraction_store = {
        **extraction,
        "_pipeline": {
            "version": PIPELINE_VERSION,
            "thanks_line": thanks_line,
            "sender_first_name": sender_name,
            "separate_email_recommended": separate if isinstance(separate, dict) else None,
            "review_table": review_table,
            "kept_body": kept_body_final,
            "show_kept_body": bool(kept_body_final and policy_blocked),
        },
    }

    # One draft per call: drop older rows for this transcript before insert
    prune_older_drafts(session, transcript_id=str(item["id"]))

    draft = CallSummaryDraft(
        transcript_id=str(item["id"]),
        transcript_name=item.get("name"),
        account_name=(account or {}).get("name") if account else None,
        subject=str(composition.get("subject") or "(no subject)"),
        body_text=str(composition.get("body") or ""),
        reviewer_notes=reviewer_notes,
        extraction_json=extraction_store,
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


def _call_date_yyyy_mm_dd(event_start_time: str | None) -> str:
    raw = (event_start_time or "").strip()
    return raw[:10] if len(raw) >= 10 else ""


def list_matching_calls(
    session: Session,
    *,
    search: str = "",
    account: str = "",
    designer: str = "",
    date_from: str = "",
    date_to: str = "",
    draft: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, dict]:
    """Fetch slim transcripts for designer emails, filter to external, optional search/filters.

    Returns (page, total, facets) where facets has sorted ``accounts`` and ``designers``
    lists derived from the search-matched pool (before account/designer/date/draft filters).
    """
    from designops.adapters.transcript_api import list_transcripts

    empty_facets: dict = {"accounts": [], "designers": []}
    designers = resolve_designer_emails(session)
    emails = [str(d["email"]) for d in designers if d.get("email")]
    if not emails:
        return [], 0, empty_facets

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
            account_obj = t.get("account") if isinstance(t.get("account"), dict) else {}
            row = {
                "id": t.get("id"),
                "name": t.get("name") or "",
                "account_name": (account_obj or {}).get("name"),
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

    draft_map = latest_draft_ids_by_transcript(session)
    for row in collected:
        row["draft_id"] = draft_map.get(str(row.get("id") or ""))

    account_names = sorted(
        {(r.get("account_name") or "").strip() or "UNASSIGNED" for r in collected},
        key=lambda s: (s == "UNASSIGNED", s.lower()),
    )
    designer_opts: dict[str, str] = {}
    for r in collected:
        for d in r.get("designer_attendees") or []:
            email = (d.get("email") or "").strip().lower()
            if not email:
                continue
            label = (d.get("name") or "").strip() or email
            # Prefer a real name over bare email if we see both
            prev = designer_opts.get(email)
            if not prev or ("@" in prev and "@" not in label):
                designer_opts[email] = label
    designer_facet = sorted(
        [{"email": e, "label": designer_opts[e]} for e in designer_opts],
        key=lambda x: (x["label"] or "").lower(),
    )
    facets = {"accounts": account_names, "designers": designer_facet}

    account_f = (account or "").strip()
    designer_f = (designer or "").strip().lower()
    date_from_f = (date_from or "").strip()[:10]
    date_to_f = (date_to or "").strip()[:10]
    draft_f = (draft or "").strip().lower()
    if draft_f not in ("", "yes", "no", "all"):
        draft_f = ""
    if draft_f == "all":
        draft_f = ""

    filtered: list[dict] = []
    for row in collected:
        acct = (row.get("account_name") or "").strip() or "UNASSIGNED"
        if account_f and acct != account_f:
            continue
        if designer_f:
            emails_names = [
                ((d.get("email") or "").lower(), (d.get("name") or "").lower())
                for d in (row.get("designer_attendees") or [])
            ]
            if not any(designer_f == e or designer_f in n for e, n in emails_names):
                continue
        day = _call_date_yyyy_mm_dd(row.get("event_start_time"))
        if date_from_f and (not day or day < date_from_f):
            continue
        if date_to_f and (not day or day > date_to_f):
            continue
        has_draft = bool(row.get("draft_id"))
        if draft_f == "yes" and not has_draft:
            continue
        if draft_f == "no" and has_draft:
            continue
        filtered.append(row)

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return page, total, facets
