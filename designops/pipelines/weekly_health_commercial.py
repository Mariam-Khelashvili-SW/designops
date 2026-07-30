"""Fairwind Salesforce commercial context for weekly health.

Pulls agreements + opportunities (optionally with invoices in one export) and
derives display subtitle / SOW facts. Signed design hours are only used when
Fairwind exposes a real hours figure — never invented from Jira or seeds.

When reuse=True, persists under CORPUS_STORE_DIR/weekly_health/<date_to>/<account>/
so a second generate for the same week skips Fairwind.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from designops.adapters.fairwind import FairwindClient
from designops.core.config import Settings, get_settings
from designops.pipelines.weekly_health_invoices import (
    _create_export_with_retry,
    parse_invoices_from_zip,
)

_COMMERCIAL_TYPES = [
    "salesforce_invoices",
    "salesforce_agreements",
    "salesforce_opportunities",
]

# Prefer delivery SOWs over NDAs / MSAs / notices for the project subtitle.
_SOW_TYPE_RE = re.compile(
    r"sow|statement of work|discovery agreement|fixed project|agile project|"
    r"phase\s*\d|discovery",
    re.I,
)
_SKIP_TYPE_RE = re.compile(
    r"\bnda\b|non-disclosure|master service|reorganization|hosting|support agreement",
    re.I,
)
_WON_STAGES = frozenset(
    {
        "won",
        "closed won",
        "closedwon",
    }
)

# Cold pulls: keep the window short enough that Fairwind builds quickly, long
# enough to catch current SOWs + unpaid UX invoices.
_COMMERCIAL_YEARS = 3
_POLL_INTERVAL_S = 2


def commercial_cache_path(
    settings: Settings, account_id: str, date_to: date
) -> Path:
    return (
        Path(settings.corpus_store_dir)
        / "weekly_health"
        / date_to.isoformat()
        / account_id
        / "commercial.json"
    )


def load_commercial_cache(
    settings: Settings, account_id: str, date_to: date
) -> dict[str, list[dict]] | None:
    path = commercial_cache_path(settings, account_id, date_to)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "invoices": list(data.get("invoices") or []),
        "agreements": list(data.get("agreements") or []),
        "opportunities": list(data.get("opportunities") or []),
    }


def save_commercial_cache(
    settings: Settings,
    account_id: str,
    date_to: date,
    payload: dict[str, list[dict]],
) -> Path:
    path = commercial_cache_path(settings, account_id, date_to)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date_to": date_to.isoformat(),
                "account_id": account_id,
                "invoices": payload.get("invoices") or [],
                "agreements": payload.get("agreements") or [],
                "opportunities": payload.get("opportunities") or [],
            }
        )
    )
    return path


def commercial_date_from(date_to: date) -> date:
    return date(max(2020, date_to.year - _COMMERCIAL_YEARS), 1, 1)


def parse_agreements_from_zip(blob: bytes) -> list[dict]:
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if not name.startswith("json/salesforce/agreements/"):
                continue
            if not name.endswith("agreement.json"):
                continue
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("name"):
                out.append(data)
    return out


def parse_opportunities_from_zip(blob: bytes) -> list[dict]:
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if not name.startswith("json/salesforce/opportunities/"):
                continue
            if not name.endswith(".json") or name.endswith("index.json"):
                continue
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("name"):
                out.append(data)
    return out


def short_agreement_title(name: str) -> str:
    """Strip Fairwind naming prefix → human subtitle."""
    n = (name or "").strip()
    if not n:
        return ""
    if ")_" in n:
        return n.rsplit(")_", 1)[-1].strip()
    parts = n.split("_", 3)
    if len(parts) == 4 and parts[0].isdigit() and len(parts[0]) == 4:
        return parts[3].replace("_", " ").strip()
    return n


def _parse_doc_date(raw: Any) -> date | None:
    if not raw:
        return None
    s = str(raw).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _money(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _hours(v: Any) -> float | None:
    h = _money(v)
    if h is None or h <= 0:
        return None
    return h


def pick_primary_agreement(agreements: list[dict]) -> dict | None:
    scored: list[tuple[int, date, dict]] = []
    for a in agreements:
        dtype = str(a.get("document_type") or "")
        name = str(a.get("name") or "")
        blob = f"{dtype} {name}"
        if _SKIP_TYPE_RE.search(blob) and not _SOW_TYPE_RE.search(blob):
            continue
        score = 0
        if _SOW_TYPE_RE.search(blob):
            score += 10
        if re.search(r"phase\s*\d|sow", blob, re.I):
            score += 5
        if _SKIP_TYPE_RE.search(dtype):
            score -= 8
        d = _parse_doc_date(a.get("document_date")) or date.min
        scored.append((score, d, a))
    if not scored:
        dated = [
            (_parse_doc_date(a.get("document_date")) or date.min, a)
            for a in agreements
            if a.get("name")
        ]
        if not dated:
            return None
        dated.sort(key=lambda x: x[0], reverse=True)
        return dated[0][1]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def _opp_is_forecast_noise(name: str) -> bool:
    """Monthly billing/forecast artifacts — never project SOW opportunities.

    'Dedicated in <month>' / 'INV OPP' rows carry whole-team monthly resource
    hours, which must never masquerade as a signed design estimate.
    """
    n = name.lower()
    return (
        "kam forecast" in n
        or "on-site" in n
        or "inv opp" in n
        or "invoicing opportunity" in n
        or bool(re.search(r"\bdedicated in\b", n))
    )


def pick_primary_opportunity(
    opportunities: list[dict], *, jira_key: str | None = None
) -> dict | None:
    key = (jira_key or "").strip().upper()
    scored: list[tuple[int, float, dict]] = []
    for o in opportunities:
        name = str(o.get("name") or "")
        if not name or _opp_is_forecast_noise(name):
            continue
        stage = str(o.get("stage_name") or "").strip().lower()
        score = 0
        if key and str(o.get("jira_key") or "").strip().upper() == key:
            score += 20
        if stage in _WON_STAGES:
            score += 15
        elif "negotiation" in stage or "review" in stage:
            score += 5
        if re.search(r"phase\s*\d|discovery|ux|redesign|sow", name, re.I):
            score += 8
        coop = str(o.get("cooperation") or "")
        if coop:
            score += 2
        amount = _money(o.get("amount")) or 0.0
        scored.append((score, amount, o))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def signed_hours_from_opportunities(
    opportunities: list[dict], *, jira_key: str | None = None
) -> float | None:
    """Only return hours Fairwind stores as resource/estimate totals — never Jira."""
    key = (jira_key or "").strip().upper()
    best: float | None = None
    for o in opportunities:
        name = str(o.get("name") or "")
        if not name or _opp_is_forecast_noise(name):
            continue
        stage = str(o.get("stage_name") or "").strip().lower()
        if stage and stage not in _WON_STAGES and "negotiation" not in stage:
            continue
        if key and str(o.get("jira_key") or "").strip().upper() not in ("", key):
            if str(o.get("jira_key") or "").strip():
                continue
        for field in ("hours_sum_resources",):
            h = _hours(o.get(field))
            if h is not None and (best is None or h > best):
                best = h
    return best


def summarize_live_project_meta(
    agreements: list[dict],
    opportunities: list[dict],
    *,
    jira_key: str | None = None,
) -> dict:
    """Build subtitle + agreement facts + optional signed hours from Fairwind."""
    primary_a = pick_primary_agreement(agreements)
    primary_o = pick_primary_opportunity(opportunities, jira_key=jira_key)

    subtitle = ""
    agreement: dict[str, Any] = {}

    if primary_a:
        title = short_agreement_title(str(primary_a.get("name") or ""))
        subtitle = title
        agreement["sow_name"] = title
        agreement["document_type"] = primary_a.get("document_type")
        d = _parse_doc_date(primary_a.get("document_date"))
        if d:
            agreement["document_date"] = d.isoformat()

    if primary_o:
        opp_name = str(primary_o.get("name") or "").strip()
        if opp_name:
            agreement["opportunity"] = opp_name
            if not subtitle:
                subtitle = re.sub(
                    r"^.*?-\s*",
                    "",
                    opp_name.split("|")[0].strip(),
                    count=1,
                ).strip() or opp_name
        stage = primary_o.get("stage_name")
        if stage:
            agreement["stage"] = stage
        coop = primary_o.get("cooperation")
        if coop:
            agreement["cooperation"] = coop
            agreement["contract_type"] = str(coop).lower()
        amount = _money(primary_o.get("amount"))
        if amount is not None and amount > 0:
            agreement["amount_eur"] = amount
        jira = primary_o.get("jira_key")
        if jira:
            agreement["jira_key"] = jira

    signed_h = signed_hours_from_opportunities(opportunities, jira_key=jira_key)

    return {
        "subtitle": subtitle,
        "agreement": agreement,
        "signed_design_estimate_h": signed_h,
        "source": "fairwind",
    }


def _parse_commercial_zip(blob: bytes) -> dict[str, list[dict]]:
    return {
        "invoices": parse_invoices_from_zip(blob),
        "agreements": parse_agreements_from_zip(blob),
        "opportunities": parse_opportunities_from_zip(blob),
    }


def _fetch_commercial_live(
    client: FairwindClient,
    account_id: str,
    *,
    start: date,
    date_to: date,
) -> dict[str, list[dict]]:
    empty = {"invoices": [], "agreements": [], "opportunities": []}
    try:
        export_id = _create_export_with_retry(
            client,
            account_id,
            start,
            date_to,
            data_types=list(_COMMERCIAL_TYPES),
        )
    except httpx.HTTPStatusError:
        export_id = None

    if export_id:
        client.poll_export(export_id, interval_s=_POLL_INTERVAL_S)
        return _parse_commercial_zip(client.download_zip(export_id))

    # Combined export empty/rejected (often one missing type) — pull per type so
    # missing opportunities don't wipe invoices/agreements.
    out = dict(empty)
    type_map = [
        ("salesforce_invoices", "invoices", parse_invoices_from_zip),
        ("salesforce_agreements", "agreements", parse_agreements_from_zip),
        ("salesforce_opportunities", "opportunities", parse_opportunities_from_zip),
    ]
    for dtype, key, parser in type_map:
        try:
            eid = _create_export_with_retry(
                client, account_id, start, date_to, data_types=[dtype]
            )
        except httpx.HTTPStatusError:
            continue
        if not eid:
            continue
        client.poll_export(eid, interval_s=_POLL_INTERVAL_S)
        out[key] = parser(client.download_zip(eid))
    return out


def fetch_salesforce_commercial(
    client: FairwindClient,
    account_id: str,
    *,
    date_to: date,
    date_from: date | None = None,
    reuse: bool = True,
    settings: Settings | None = None,
) -> tuple[dict[str, list[dict]], str]:
    """Return (commercial_payload, source) where source is fairwind|fairwind-cached."""
    s = settings or getattr(client, "s", None) or get_settings()
    start = date_from or commercial_date_from(date_to)
    if reuse:
        cached = load_commercial_cache(s, account_id, date_to)
        if cached is not None:
            return cached, "fairwind-cached"

    payload = _fetch_commercial_live(client, account_id, start=start, date_to=date_to)
    save_commercial_cache(s, account_id, date_to, payload)
    return payload, "fairwind"
