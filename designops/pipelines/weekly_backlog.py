"""A3 Weekly Planning Board — DB-backed orchestration.

Monday brief for Olga: weekly load = In Progress + To Do remaining hours
plus person-level dedicated weekly hours; Client Action listed in detail;
other assigned tickets collapsed. LLM phrases rebalance moves + flag notes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from designops.adapters.delivery import deliver
from designops.adapters.documents import Document
from designops.adapters.jira import JiraClient, resolve_roster_account_ids
from designops.adapters.llm import LLMClient, parse_digest_json
from designops.core.config import get_settings
from designops.core.enums import FlagType, RunStatus, SendMode
from designops.core.identity import RosterIndex
from designops.core.models import (
    Account,
    Artifact,
    Flag,
    IngestBatch,
    Person,
    Pipeline,
    PipelineRun,
    Project,
    RunDocument,
)
from designops.core.projects import enable_accounts_for_jira_keys, jira_project_keys_from_docs
from designops.core.registry import ProjectRegistry
from designops.pipelines.daily_digest import _ingest
from designops.pipelines.email_subjects import email_subject_for_pipeline
from designops.pipelines.filter import FilterResult, filter_corpus
from designops.pipelines.render import render_weekly_backlog
from designops.pipelines.weekly_availability import (
    at_a_glance_kpis,
    availability_marker,
    board_sort_key,
    build_person_board_row,
    default_flagline,
    doc_to_ticket,
    extract_ticket_keys,
    filter_work_tickets,
    previous_friday,
    resolve_week_monday,
)

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "weekly-backlog.md"
PIPELINE_KEY = "weekly-backlog"
_TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _now() -> datetime:
    return datetime.now(UTC)


def _friday_plan_text(docs: list[Document]) -> str:
    """Concatenate a person's Friday daily(s) for key extraction (§4)."""
    parts = []
    for d in docs:
        body = (d.body or "").strip()
        title = (d.title or "").strip()
        if body:
            parts.append(f"{title}\n{body}" if title else body)
    return "\n\n---\n\n".join(parts).strip()


def _tickets_by_key(docs: list[Document]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in docs:
        t = doc_to_ticket(d)
        key = (t.get("key") or "").upper()
        if key:
            out[key] = t
    return out


_NARRATIVE_STOP = frozenset(
    {
        "with",
        "from",
        "that",
        "this",
        "have",
        "will",
        "been",
        "were",
        "they",
        "them",
        "then",
        "than",
        "into",
        "over",
        "after",
        "before",
        "about",
        "today",
        "tomorrow",
        "week",
        "next",
        "work",
        "working",
        "ticket",
        "tickets",
        "task",
        "tasks",
        "page",
        "pages",
        "update",
        "updates",
        "fix",
        "fixes",
        "final",
        "report",
        "daily",
        "friday",
        "monday",
        "done",
        "plan",
        "planned",
    }
)


def _active_assigned(assigned_tickets: list[dict]) -> list[dict]:
    """All open assigned work tickets (hardware / time-log buckets dropped).

    Weekly load filtering (In Progress / To Do) happens in build_person_board_row;
    Client Action stays detailed; everything else collapses to one summary line.
    """
    return filter_work_tickets(assigned_tickets)


def _tickets_matching_narrative(friday_text: str, candidates: list[dict]) -> list[dict]:
    """Match plan wording to ticket summaries / names (before project fallback)."""
    from designops.core.registry import normalize

    text_n = normalize(friday_text or "")
    if not text_n or not candidates:
        return []
    matched: list[dict] = []
    seen: set[str] = set()
    for t in candidates:
        key = (t.get("key") or "").upper()
        summary = (t.get("summary") or "").strip()
        if not summary or summary.startswith("("):
            continue
        sn = normalize(summary)
        hit = False
        # Prefer a clear summary phrase appearing in the report.
        if len(sn) >= 8 and sn in text_n:
            hit = True
        else:
            tokens = [
                w
                for w in re.findall(r"[a-z0-9]{4,}", sn)
                if w not in _NARRATIVE_STOP
            ]
            if tokens:
                hits = sum(1 for w in tokens if w in text_n)
                if len(tokens) == 1:
                    hit = hits == 1 and len(tokens[0]) >= 6
                else:
                    hit = hits >= max(2, (len(tokens) + 1) // 2)
        if hit and key not in seen:
            seen.add(key or summary)
            matched.append(t)
    return matched


def _tickets_matching_projects(
    friday_text: str,
    candidates: list[dict],
    registry: ProjectRegistry | None,
) -> list[dict]:
    """Match projects named in the plan → assigned tickets on those Jira keys.

    Two sources:
    1) Registry aliases (canonical names, alt names)
    2) Jira project_name from the tickets themselves — lets us match even when
       the registry has no entry, e.g. "Laderach" in the report matching
       "SC Dedicated team - Laderach" from the Jira project name.
    """
    from designops.core.registry import normalize

    if not friday_text.strip() or not candidates:
        return []
    text_n = normalize(friday_text)
    project_keys: set[str] = set()

    # 1) Registry aliases
    if registry:
        for alias, entry in registry.aliases_longest_first():
            if len(alias) < 3 or not entry.jira_project_key:
                continue
            if len(alias) <= 3:
                if not re.search(rf"\b{re.escape(alias)}\b", text_n):
                    continue
            elif alias not in text_n:
                continue
            project_keys.add(entry.jira_project_key.upper())

    # 2) Jira project_name tokens from the tickets themselves
    pname_to_key: dict[str, str] = {}
    for t in candidates:
        pname = t.get("project_name") or ""
        pkey = (t.get("project_key") or "").upper()
        if pkey and pkey not in project_keys:
            pname_to_key.setdefault(pkey, pname)
    for pkey, pname in pname_to_key.items():
        # Match Jira project key in text (e.g. "WIEND" or "ESELO")
        if pkey and len(pkey) >= 3 and re.search(rf"\b{re.escape(pkey.lower())}\b", text_n):
            project_keys.add(pkey)
            continue
        if not pname:
            continue
        pname_n = normalize(pname)
        words = [w for w in re.findall(r"[a-z]{5,}", pname_n)]
        for w in words:
            if w in text_n:
                project_keys.add(pkey)
                break

    if not project_keys:
        return []
    return [
        t
        for t in candidates
        if (t.get("project_key") or "").upper() in project_keys
    ]


def _select_planned_tickets(
    *,
    friday_text: str,
    assigned_tickets: list[dict],
    all_by_key: dict[str, dict],
    availability: str,
    registry: ProjectRegistry | None = None,
) -> tuple[list[dict], list[str], bool]:
    """Resolve assigned work tickets for the person board.

    Returns all open assigned work tickets (load/detail/other partitioning
    happens in build_person_board_row). Friday keys are informational only.

    Returns (tickets, friday_keys, no_plan).
    """
    if availability == "OUT":
        return [], [], False

    friday_keys = extract_ticket_keys(friday_text)
    active = _active_assigned(assigned_tickets)
    return active, friday_keys, len(active) == 0


def _build_person_rows(
    roster_rows: list[Person],
    week_monday: date,
    friday_docs_by_person: dict,
    jira_docs_by_person: dict,
    all_jira_by_key: dict[str, dict],
    normal_hours: float,
    registry: ProjectRegistry | None = None,
) -> list[dict]:
    rows = []
    for p in roster_rows:
        avail = availability_marker(p.status, p.leave_until, week_monday)
        jdocs = jira_docs_by_person.get(p.id, [])
        fdocs = friday_docs_by_person.get(p.id, [])
        friday_text = _friday_plan_text(fdocs)
        assigned = [doc_to_ticket(d) for d in jdocs]
        tickets, friday_keys, no_plan = _select_planned_tickets(
            friday_text=friday_text,
            assigned_tickets=assigned,
            all_by_key=all_jira_by_key,
            availability=avail,
            registry=registry,
        )
        
        has_friday_report = bool(fdocs) or bool(friday_text.strip())
        dedicated_h = getattr(p, "dedicated_weekly_hours", None)
        is_dedicated = bool(getattr(p, "is_dedicated", False))
        row = build_person_board_row(
            name=p.full_name,
            availability=avail,
            tickets=tickets,
            capacity=normal_hours,
            has_friday_plan=has_friday_report,
            friday_keys=friday_keys,
            no_plan=no_plan and avail != "OUT",
            is_dedicated=is_dedicated,
            dedicated_weekly_hours=dedicated_h,
        )

        row["friday_excerpt"] = friday_text[:4000] if friday_text else ""
        row["person_id"] = str(p.id)
        rows.append(row)
    _apply_rich_flaglines(rows, normal_hours)
    return rows


def _fmt_plan_hours(v: float | None) -> str:
    if v is None:
        return "?"
    r = round(float(v), 2)
    return str(int(r)) if r == int(r) else f"{r:g}"


def _workable_tickets(tickets: list[dict]) -> list[dict]:
    from designops.pipelines.weekly_availability import is_blocked_status

    out = []
    for t in tickets or []:
        if is_blocked_status(t.get("status")):
            continue
        left = float(t.get("remaining_hours") or 0)
        if left <= 0:
            continue
        out.append(t)
    out.sort(key=lambda t: float(t.get("remaining_hours") or 0), reverse=True)
    return out


def _heaviest_clusters(tickets: list[dict], *, max_tickets: int = 5) -> str:
    """e.g. 'Acer Client Action (ACERP1-35 13h, ACERP1-40 7h); SGDCP-29 5h'."""
    top = _workable_tickets(tickets)[:max_tickets]
    if not top:
        return ""
    by_proj: dict[str, list[dict]] = {}
    for t in top:
        pk = (t.get("project_key") or t.get("project_name") or "?").upper()
        by_proj.setdefault(pk, []).append(t)
    parts: list[str] = []
    for _pk, group in by_proj.items():
        name = (group[0].get("project_name") or group[0].get("project_key") or "").strip()
        status = (group[0].get("status_display") or group[0].get("status") or "").strip()
        inner = ", ".join(
            f"{t.get('key')} {_fmt_plan_hours(t.get('remaining_hours'))}h" for t in group
        )
        if len(group) == 1 and not name:
            parts.append(inner)
            continue
        label = name or _pk
        # Mention Client Action / In Progress when it's the load driver.
        st_l = status.lower()
        if st_l and st_l not in {"to do", "new", "open", "backlog"}:
            label = f"{label} {status}"
        parts.append(f"{label} ({inner})")
    return "; ".join(parts)


def _rich_flagline(person: dict, *, capacity: float, peers: list[dict]) -> dict:
    """Code-side coaching note that cites load + heaviest tickets (and peers)."""
    if person.get("availability") == "OUT":
        return {"kind": "", "lab": "", "text": ""}

    planned = float(person.get("planned_hours") or 0)
    blocked = float(person.get("blocked_hours") or 0)
    band = person.get("band") or ""
    tickets = person.get("tickets") or []
    heaviest = _heaviest_clusters(tickets)

    spares = [
        x
        for x in peers
        if x.get("band") == "SPARE"
        and x.get("availability") != "OUT"
        and x.get("name") != person.get("name")
    ]
    spare_name = spares[0]["name"].split()[0] if spares else ""

    if band == "OVER_PLANNED":
        lab = "Trim plan" if planned >= capacity * 1.5 else "Overloaded"
        text = f"Over-planned at {_fmt_plan_hours(planned)}h."
        if heaviest:
            text += f" Heaviest on {heaviest}."
        if spare_name:
            text += f" Consider shifting some work to {spare_name}."
        return {"kind": "over", "lab": lab, "text": text}

    if band == "IDLE" and blocked > 0:
        return {
            "kind": "idle",
            "lab": "Unblock",
            "text": (
                f"Blocked → idle ({_fmt_plan_hours(blocked)}h blocked). "
                "Zero workable hours until dependencies clear."
            ),
        }
    if band == "IDLE":
        return {
            "kind": "idle",
            "lab": "Idle",
            "text": "No planned workable hours this week.",
        }
    if band == "SPARE":
        spare = max(0.0, capacity - planned)
        text = (
            f"Spare ~{_fmt_plan_hours(spare)}h "
            f"({_fmt_plan_hours(planned)}h of {_fmt_plan_hours(capacity)}h planned)."
        )
        if heaviest:
            text += f" Current focus: {heaviest}."
        text += " Clear place to route overflow."
        return {"kind": "idle", "lab": "Take work", "text": text}

    if band == "AT_CAPACITY":
        text = f"At capacity ({_fmt_plan_hours(planned)}h)."
        dedic = person.get("dedicated_weekly_hours")
        if dedic:
            text += f" Includes {_fmt_plan_hours(float(dedic))}h dedicated."
        if heaviest:
            text += f" Heaviest: {heaviest}."
        return {"kind": "bal", "lab": "At capacity", "text": text}

    return {"kind": "", "lab": "", "text": ""}


def _apply_rich_flaglines(people: list[dict], capacity: float) -> None:
    for p in people:
        p["flagline"] = _rich_flagline(p, capacity=capacity, peers=people)


def _fallback_rebalance(people: list[dict], capacity: float) -> dict:
    overs = [p for p in people if p.get("band") == "OVER_PLANNED"]
    idles = [p for p in people if p.get("band") == "IDLE"]
    spares = [p for p in people if p.get("band") == "SPARE"]
    moves = []
    if overs:
        top = overs[0]
        moves.append(
            {
                "text": (
                    f"Cut {top['name'].split()[0]}'s plan down to a week. "
                    f"{top['planned_hours']}h named — decide what's actually this week."
                ),
                "project": "capacity",
            }
        )
    if idles:
        p = idles[0]
        moves.append(
            {
                "text": (
                    f"Unblock {p['name'].split()[0]} — "
                    f"{'Blocked → idle' if p.get('blocked_hours') else 'idle'} "
                    f"({p.get('blocked_hours', 0)}h blocked)."
                ),
                "project": "blocked",
            }
        )
    if spares:
        p = spares[0]
        spare = max(0, capacity - float(p.get("planned_hours") or 0))
        moves.append(
            {
                "text": (
                    f"Route ~{spare:.0f}h to {p['name'].split()[0]}. "
                    f"Only {p['planned_hours']}h of {capacity:.0f} booked."
                ),
                "project": "capacity",
            }
        )
    team_planned = sum(
        float(p.get("planned_hours") or 0)
        for p in people
        if p.get("availability") != "OUT"
    )
    avail_n = sum(1 for p in people if p.get("availability") != "OUT")
    team_cap = avail_n * capacity
    over_h = max(0, team_planned - team_cap)
    return {
        "title": f"Before Monday — {len(moves)} moves" if moves else "Before Monday",
        "subtitle": (
            f"The team planned ~{over_h:.0f}h more than it can do this week. "
            "Trim the top, unblock the idle, fill the spare."
            if over_h > 0
            else "Rebalance where the board shows spare or blocked → idle."
        ),
        "moves": moves[:4],
    }


def _scrub_transient_person_fields(people_rows: list[dict]) -> None:
    for p in people_rows:
        p.pop("friday_excerpt", None)
        p.pop("person_id", None)


def _synthesize_coaching(
    people_rows: list[dict],
    week_monday: date,
    friday_date: date,
    capacity: float,
) -> tuple[list[dict], dict, dict]:
    """LLM phrases rebalance moves + flag notes; falls back to code defaults."""
    settings = get_settings()
    rebalance = _fallback_rebalance(people_rows, capacity)

    if not people_rows:
        raise RuntimeError(
            "Roster is empty — run `python -m scripts.seed` (or restore a DB dump) "
            "before generating the weekly backlog."
        )

    if not settings.anthropic_configured:
        _scrub_transient_person_fields(people_rows)
        return people_rows, rebalance, {
            "mode": "fallback",
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "note": "No ANTHROPIC_API_KEY — used code-side rebalance + flaglines.",
        }

    skill = _SKILL.read_text(encoding="utf-8")
    roster_block = "\n".join(
        f"- {p['name']} ({p['availability']}; "
        f"{p['planned_hours']}h planned / {p['blocked_hours']}h blocked; "
        f"band={p['band']}"
        f")"
        for p in people_rows
    )
    system = (
        skill.replace("{week_of}", week_monday.isoformat())
        .replace("{friday_date}", friday_date.isoformat())
        .replace("{roster}", roster_block)
        .replace("{normal_week_hours}", str(capacity))
    )
    chunks = [
        f"Week of {week_monday.isoformat()} (Friday plans from {friday_date.isoformat()}).",
        f"PER-PERSON DATA follows ({len(people_rows)} people). "
        "Write rebalance moves and per-person flag notes only. Hours/bands are already set.",
        "Flag notes MUST cite the heaviest tickets (key + hours) and, when a Friday report",
        "exists, weave in what they said they would work on. Example style:",
        '"Over-planned at 48.75h. Heaviest on Acer Client Action wireframes '
        '(ACERP1-35 13h, ACERP1-40 7h) plus SGDCP-29 5h; consider shifting some Acer work to Arturs."',
        "",
        "Roster summary:",
        roster_block,
        "",
    ]
    for p in people_rows:
        chunks.append(
            f"### {p['name']} — {p['availability']} · {p['band']} · "
            f"{p['planned_hours']}h planned / {capacity}h · {p['blocked_hours']}h blocked"
        )
        if p.get("friday_excerpt"):
            chunks.append("Friday daily:")
            chunks.append(p["friday_excerpt"][:2000])
        else:
            chunks.append("Friday daily: (none)")
        for g in p.get("status_groups") or []:
            chunks.append(
                f"[{g['status']}] {g['count']} · {g['left_hours']}h left"
            )
            for t in g.get("tickets") or []:
                chunks.append(
                    f"  - {t.get('key')}: {t.get('summary')} "
                    f"(est={t.get('original_hours')}; log={t.get('spent_hours')}; "
                    f"left={t.get('remaining_hours')})"
                )
        chunks.append("")
    chunks.append(
        "Respond with a single JSON object only (schema in the system prompt). No prose."
    )
    user = "\n".join(chunks)

    try:
        result = LLMClient(settings).synthesize(
            system=system, user_content=user, max_tokens=6000
        )
        parsed = parse_digest_json(result.text)
    except Exception as e:  # noqa: BLE001 — coaching is optional; board numbers are code-side
        _scrub_transient_person_fields(people_rows)
        return people_rows, rebalance, {
            "mode": "fallback",
            "model": settings.digest_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "note": f"LLM coaching failed ({type(e).__name__}: {e}); used code-side rebalance.",
        }

    rb = parsed.get("rebalance") if isinstance(parsed.get("rebalance"), dict) else None
    if rb and (rb.get("moves") or rb.get("title")):
        moves = []
        for m in rb.get("moves") or []:
            if isinstance(m, dict) and m.get("text"):
                moves.append(
                    {
                        "text": str(m["text"]).strip(),
                        "project": str(m.get("project") or "").strip(),
                    }
                )
            elif isinstance(m, str) and m.strip():
                moves.append({"text": m.strip(), "project": ""})
        if moves:
            rebalance = {
                "title": str(rb.get("title") or rebalance["title"]).strip(),
                "subtitle": str(rb.get("subtitle") or rebalance["subtitle"]).strip(),
                "moves": moves[:6],
            }

    by_name = {
        str(x.get("name", "")).strip(): x
        for x in (parsed.get("people") or [])
        if isinstance(x, dict)
    }
    for p in people_rows:
        note = by_name.get(p["name"])
        if note:
            lab = str(note.get("flag_label") or note.get("lab") or "").strip()
            text = str(note.get("flag_note") or note.get("text") or "").strip()
            allowed = {t["key"] for t in p.get("tickets") or [] if t.get("key")}

            def _scrub(m: re.Match) -> str:
                return m.group(0) if m.group(1) in allowed else ""

            if text and allowed:
                text = _TICKET_KEY_RE.sub(_scrub, text)
                text = re.sub(r"\s{2,}", " ", text).strip(" ·,-")
            if lab and text:
                kind = p["flagline"].get("kind") or ""
                if p.get("band") == "OVER_PLANNED":
                    kind = "over"
                elif p.get("band") in ("IDLE", "SPARE"):
                    kind = "idle"
                elif p.get("band") == "AT_CAPACITY":
                    kind = "bal"
                p["flagline"] = {"kind": kind, "lab": lab, "text": text}
    _scrub_transient_person_fields(people_rows)

    return people_rows, rebalance, {
        "mode": "llm",
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": float(result.cost_usd),
    }


def _is_sample_run(pipeline: Pipeline, send_mode_override: str | None) -> bool:
    """SAMPLE banner only when delivery will not leave the system."""
    mode = (send_mode_override or pipeline.send_mode or SendMode.NONE.value).lower()
    if not pipeline.go_live:
        return True
    return mode in {SendMode.NONE.value, "none", ""}


def create_pending_run(session: Session, week_monday: date) -> PipelineRun:
    pipeline = session.query(Pipeline).filter_by(key=PIPELINE_KEY).one()
    run = PipelineRun(
        pipeline_id=pipeline.id,
        report_date=week_monday,
        started_at=_now(),
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return run


def execute_run(
    session: Session,
    run: PipelineRun,
    *,
    reuse_ingest: bool = True,
    send_mode_override: str | None = None,
) -> PipelineRun:
    week_monday = resolve_week_monday(run.report_date)
    friday_date = previous_friday(week_monday)
    pipeline = session.get(Pipeline, run.pipeline_id)
    settings = get_settings()
    normal = float(settings.normal_week_hours)

    roster_rows = list(session.query(Person).all())
    project_rows = session.query(Project).all()
    roster_for_filter = RosterIndex.from_rows(
        [p for p in roster_rows if p.status != "out"], friday_date
    )
    roster_full = RosterIndex.from_rows(roster_rows, week_monday)
    registry = ProjectRegistry.from_rows(project_rows)

    friday_docs_by_person: dict = {p.id: [] for p in roster_rows}
    jira_docs_by_person: dict = {p.id: [] for p in roster_rows}
    coverage: dict = {
        "week_monday": week_monday.isoformat(),
        "friday_date": friday_date.isoformat(),
    }
    all_jira_docs: list[Document] = []
    filtered = FilterResult()

    try:
        # --- Direct Jira first (needed to know which Fairwind accounts to pull) ---
        jira_incomplete = False
        account_ids_resolved = 0
        all_jira_by_key: dict[str, dict] = {}
        client: JiraClient | None = None
        if settings.jira_configured:
            query_people = [p for p in roster_rows if p.status != "out"]
            id_map = resolve_roster_account_ids(query_people, persist=True, settings=settings)
            session.flush()
            account_ids_resolved = len(id_map)
            client = JiraClient(settings)
            week_end = week_monday + timedelta(days=6)
            all_jira_docs = client.search_open_assigned(
                list(id_map.values()),
                window_from=week_monday,
                window_to=week_end,
            )
            for doc in all_jira_docs:
                member = roster_full.resolve(doc.author_identity)
                if member is None and doc.raw.get("assignee_email"):
                    member = roster_full.resolve(doc.raw["assignee_email"])
                if member:
                    jira_docs_by_person.setdefault(member.id, []).append(doc)
                    doc.person_id = member.id
            all_jira_by_key = _tickets_by_key(all_jira_docs)
        else:
            jira_incomplete = True
            coverage["jira_note"] = "JIRA_* not configured"

        # Auto-enable Fairwind accounts for Jira projects the team is actually on.
        worked_keys = jira_project_keys_from_docs(all_jira_docs)
        newly_enabled = enable_accounts_for_jira_keys(
            session,
            worked_keys,
            enabled_by=settings.setup_owner_email or "weekly-backlog-auto",
        )
        coverage["jira_project_keys"] = sorted(worked_keys)
        coverage["accounts_auto_enabled"] = [a.name for a in newly_enabled]
        if newly_enabled:
            project_rows = session.query(Project).all()
            registry = ProjectRegistry.from_rows(project_rows)

        # --- Friday Fairwind dailies (DEV SPEC §4 planned ticket keys) ---
        documents, fri_cov = _ingest(session, friday_date, reuse=reuse_ingest)
        coverage["friday_source"] = fri_cov.get("source")
        coverage["friday_exports_failed"] = fri_cov.get("exports_failed", 0)
        coverage["friday_accounts_requested"] = fri_cov.get("accounts_requested", 0)
        account_names = {
            a.fairwind_account_id: a.name
            for a in session.query(Account).all()
            if a.fairwind_account_id
        }
        filtered = filter_corpus(
            documents, roster_for_filter, registry, friday_date, account_names=account_names
        )
        for inc in filtered.included:
            if inc.document.source == "jira":
                continue
            friday_docs_by_person.setdefault(inc.person.id, []).append(inc.document)

        batch = IngestBatch(
            report_date=friday_date,
            account_ids=[
                a.fairwind_account_id
                for a in session.query(Account).filter_by(digest_enabled=True).all()
                if a.fairwind_account_id
            ],
            started_at=_now(),
            finished_at=_now(),
            status="ok",
            doc_count=len(documents),
            coverage={**fri_cov, "pipeline": PIPELINE_KEY},
        )
        session.add(batch)
        session.flush()
        run.ingest_batch_id = batch.id

        friday_keys_all: set[str] = set()
        for p in roster_rows:
            text = _friday_plan_text(friday_docs_by_person.get(p.id, []))
            friday_keys_all.update(extract_ticket_keys(text))

        # Fetch Friday-named keys that weren't in open-assigned results
        if client is not None and friday_keys_all:
            missing = [k for k in friday_keys_all if k not in all_jira_by_key]
            if missing:
                extra = client.search_by_keys(missing, event_date=week_monday)
                all_jira_docs.extend(extra)
                for doc in extra:
                    t = doc_to_ticket(doc)
                    key = (t.get("key") or "").upper()
                    if key:
                        all_jira_by_key[key] = t
                    member = roster_full.resolve(doc.author_identity)
                    if member is None and doc.raw.get("assignee_email"):
                        member = roster_full.resolve(doc.raw["assignee_email"])
                    if member:
                        existing_keys = {
                            (doc_to_ticket(d).get("key") or "").upper()
                            for d in jira_docs_by_person.get(member.id, [])
                        }
                        if key not in existing_keys:
                            jira_docs_by_person.setdefault(member.id, []).append(doc)
                        doc.person_id = member.id

        coverage["jira_issues"] = len(all_jira_docs)
        coverage["friday_keys"] = len(friday_keys_all)
        coverage["account_ids_resolved"] = account_ids_resolved
        coverage["jira_incomplete"] = jira_incomplete
        coverage["incomplete"] = bool(
            coverage.get("friday_exports_failed", 0) > 0 or jira_incomplete
        )

        people_rows = _build_person_rows(
            roster_rows,
            week_monday,
            friday_docs_by_person,
            jira_docs_by_person,
            all_jira_by_key,
            normal,
            registry=registry,
        )
        people_rows, rebalance, synth = _synthesize_coaching(
            people_rows, week_monday, friday_date, normal
        )
        people_rows.sort(key=board_sort_key)

        glance = at_a_glance_kpis(people_rows, capacity=normal)
        digest = {
            "week_of": week_monday.isoformat(),
            "friday_date": friday_date.isoformat(),
            "at_a_glance": glance,
            "rebalance": rebalance,
            "people": people_rows,
            "capacity": normal,
        }
        html = render_weekly_backlog(
            digest,
            week_monday,
            friday_date,
            sample=_is_sample_run(pipeline, send_mode_override),
            coverage=coverage,
        )
    except Exception as e:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = _now()
        session.add(run)
        session.flush()
        return run

    run.status = (
        RunStatus.FLAGGED if coverage.get("incomplete") else RunStatus.OK
    )
    delivery = deliver(
        go_live=pipeline.go_live,
        send_mode=send_mode_override or pipeline.send_mode,
        html=html,
        recipients=pipeline.recipients,
        subject=email_subject_for_pipeline(PIPELINE_KEY, week_monday),
        setup_owner_email=settings.setup_owner_email,
    )
    run.finished_at = _now()
    run.counts = {
        "people": len(digest["people"]),
        "jira_issues": coverage.get("jira_issues", 0),
        "friday_included": len(filtered.included),
        "coverage": coverage,
    }
    run.input_tokens = synth["input_tokens"]
    run.output_tokens = synth["output_tokens"]
    run.cost_usd = synth["cost_usd"]
    run.skill_version = synth.get("model") or synth["mode"]
    run.note = synth.get("note")
    session.add(run)
    session.flush()

    for doc in all_jira_docs:
        session.add(
            RunDocument(
                run_id=run.id,
                source=doc.source,
                external_id=doc.external_id,
                event_date=doc.event_date,
                person_id=doc.person_id,
                project_id=None,
                included=True,
                exclusion_reason=None,
                title=doc.title,
            )
        )
    for a in filtered.audit:
        if a.source == "jira":
            continue
        session.add(
            RunDocument(
                run_id=run.id,
                source=a.source,
                external_id=a.external_id,
                event_date=a.event_date,
                person_id=a.person_id,
                project_id=a.project_id,
                included=a.included,
                exclusion_reason=a.exclusion_reason,
                title=a.title,
            )
        )

    session.add(
        Artifact(
            run_id=run.id,
            kind="html",
            content=html,
            delivery_status=delivery.status,
            delivered_at=_now() if delivery.status in ("self", "draft", "sent") else None,
            message_id=delivery.message_id,
        )
    )
    session.add(
        Artifact(
            run_id=run.id,
            kind="json",
            content=json.dumps(digest, indent=2),
            delivery_status="none",
        )
    )
    if coverage.get("incomplete"):
        session.add(
            Flag(
                run_id=run.id,
                type=FlagType.INGEST_GAP,
                body="Weekly backlog inputs incomplete (Fairwind and/or Jira).",
            )
        )
    return run


def run_weekly_backlog(
    session: Session,
    week_monday: date,
    *,
    reuse_ingest: bool = True,
    send_mode_override: str | None = None,
) -> PipelineRun:
    run = create_pending_run(session, resolve_week_monday(week_monday))
    return execute_run(
        session, run, reuse_ingest=reuse_ingest, send_mode_override=send_mode_override
    )
