"""Fairwind Salesforce invoices → total invoiced UX/UI summary for weekly health."""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from designops.adapters.documents import Document
from designops.adapters.fairwind import FairwindClient, parse_export_dir
from designops.pipelines.weekly_health_math import fmt_hours

# Match design-relevant Salesforce invoice line names / descriptions.
_UX_LINE_RE = re.compile(
    r"ux\s*/\s*ui|\bux/ui\b|\bux\b|\bdesign\b|cro[-\s]?ux",
    re.I,
)

def is_ux_line(line: dict) -> bool:
    blob = " ".join(
        str(x)
        for x in (
            line.get("name"),
            line.get("line_name"),
            line.get("line_service_supplied"),
            line.get("opportunity_product_name"),
            line.get("pricebook_entry_name"),
        )
        if x
    )
    return bool(_UX_LINE_RE.search(blob))


def _money(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def format_eur(amount: float) -> str:
    if amount == int(amount):
        return f"€{int(amount):,}"
    return f"€{amount:,.2f}"


def parse_invoices_from_zip(blob: bytes) -> list[dict]:
    """Load full invoice JSON objects from a Fairwind export zip."""
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if not name.startswith("json/salesforce/invoices/"):
                continue
            if not name.endswith(".json") or name.endswith("index.json"):
                continue
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("invoice_number"):
                out.append(data)
    return out


def parse_invoices_from_dir(root: Path) -> list[dict]:
    root = Path(root)
    out: list[dict] = []
    base = root / "json" / "salesforce" / "invoices"
    if not base.exists():
        # extracted under files/
        base = root / "files" / "json" / "salesforce" / "invoices"
    if not base.exists():
        return out
    for p in sorted(base.rglob("*.json")):
        if p.name == "index.json":
            continue
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("invoice_number"):
            out.append(data)
    return out


def ux_invoice_lines(invoices: list[dict]) -> list[dict]:
    """Flatten all invoices (any payment status) to UX/UI line rows."""
    rows: list[dict] = []
    for inv in invoices:
        for li in inv.get("line_items") or []:
            if not isinstance(li, dict) or not is_ux_line(li):
                continue
            amount = _money(li.get("line_total_price"))
            rows.append(
                {
                    "invoice_number": inv.get("invoice_number"),
                    "invoice_name": inv.get("name"),
                    "invoice_date": inv.get("invoice_date"),
                    "payment_status": inv.get("payment_status"),
                    "currency": inv.get("currency_iso_code") or "EUR",
                    "line_name": li.get("name"),
                    "line_service_supplied": li.get("line_service_supplied"),
                    "line_quantity": li.get("line_quantity"),
                    "amount": amount,
                }
            )
    return rows


def summarize_ux_invoiced(invoices: list[dict]) -> dict:
    """Build tile fields: total UX/UI invoiced to date (paid or not)."""
    rows = ux_invoice_lines(invoices)
    if not rows:
        return {
            "invoiced_label": "Not invoiced yet",
            "invoiced_muted": True,
            "invoiced_sub": "no UX/UI invoices in Fairwind",
            "invoice_notes": None,
            "ux_invoiced_total": 0.0,
            "ux_invoiced_hours": 0.0,
            "ux_invoice_lines": [],
        }

    total = round(sum(r["amount"] for r in rows), 2)
    # Prefer EUR formatting; Fairwind design accounts are EUR in practice.
    label = format_eur(total)
    # line_quantity is the hours billed on the line (Fairwind convention).
    hours = round(sum(_money(r.get("line_quantity")) for r in rows), 2)
    inv_nos = []
    for r in rows:
        n = r.get("invoice_number")
        if n and n not in inv_nos:
            inv_nos.append(n)
    first = rows[0]
    detail = first.get("line_service_supplied") or first.get("line_name") or "UX/UI"
    hours_s = f"{fmt_hours(hours)} · " if hours > 0 else ""
    if len(inv_nos) == 1:
        sub = f"{hours_s}{inv_nos[0]} · {detail}"
    else:
        sub = f"{hours_s}{len(inv_nos)} invoices · " + ", ".join(inv_nos[:3])
        if len(inv_nos) > 3:
            sub += f" +{len(inv_nos) - 3}"

    return {
        "invoiced_label": label,
        "invoiced_muted": False,
        "invoiced_sub": sub,
        "invoice_notes": None,
        "ux_invoiced_total": total,
        "ux_invoiced_hours": hours,
        "ux_invoice_lines": rows,
    }


def _create_export_with_retry(
    client: FairwindClient,
    account_id: str,
    date_from: date,
    date_to: date,
    *,
    data_types: list[str],
    attempts: int = 5,
) -> str | None:
    """Create Fairwind export; treat empty-range as None; retry 429 with backoff."""
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            return client.create_export_range(
                account_id,
                date_from,
                date_to,
                data_types=data_types,
            )
        except httpx.HTTPStatusError as e:
            if _is_empty_export(e):
                return None
            last_err = e
            if e.response is not None and e.response.status_code == 429:
                ra = e.response.headers.get("Retry-After")
                delay = int(ra) if (ra and ra.isdigit()) else min(8 * (2**attempt), 60)
                time.sleep(delay)
                continue
            raise
    if last_err:
        raise last_err
    return None


def fetch_salesforce_invoices(
    client: FairwindClient,
    account_id: str,
    *,
    date_to: date,
    date_from: date | None = None,
) -> list[dict]:
    """Pull salesforce_invoices for an account. Empty range → []."""
    start = date_from or date(max(2020, date_to.year - 3), 1, 1)
    export_id = _create_export_with_retry(
        client,
        account_id,
        start,
        date_to,
        data_types=["salesforce_invoices"],
    )
    if not export_id:
        return []
    client.poll_export(export_id, interval_s=2)
    blob = client.download_zip(export_id)
    return parse_invoices_from_zip(blob)


_EMPTY_EXPORT_CODES = frozenset(
    {
        "no_salesforce_invoices_in_range",
        "no_salesforce_agreements_in_range",
        "no_salesforce_opportunities_in_range",
        "no_data_in_range",
        "no_emails_in_range",
        "no_transcripts_in_range",
    }
)


def _is_empty_export(exc: httpx.HTTPStatusError) -> bool:
    if exc.response is None or exc.response.status_code != 400:
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    code = str(body.get("code") or "")
    msg = str(body.get("error") or "").lower()
    if code in _EMPTY_EXPORT_CODES:
        return True
    return "no " in msg and ("in range" in msg or "date range" in msg)


def _filter_week_comms(
    docs: list[Document], *, week_monday: date, report_friday: date
) -> list[Document]:
    out: list[Document] = []
    for d in docs:
        if d.event_date < week_monday or d.event_date > report_friday:
            continue
        folder = (d.raw or {}).get("folder")
        if d.source == "transcript" or folder == "external":
            out.append(d)
        elif d.source == "fairwind" and folder is None:
            # defensive: treat untagged fairwind docs in an emails_external export as external
            out.append(d)
    return out


def _comms_store_root(client: FairwindClient, account_id: str, week_monday: date) -> Path:
    return Path(client.s.corpus_store_dir) / account_id / f"health_comms_{week_monday.isoformat()}"


def load_week_client_comms_cache(
    client: FairwindClient,
    account_id: str,
    *,
    week_monday: date,
    report_friday: date,
) -> list[Document] | None:
    """Reuse a previously extracted week-comms export, or None if missing."""
    root = _comms_store_root(client, account_id, week_monday)
    if (root / ".empty").exists():
        return []
    files_root = root / "files"
    zip_path = root / "export.zip"
    if not files_root.exists() and zip_path.exists():
        files_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(files_root)
    if not files_root.exists():
        return None
    docs = parse_export_dir(files_root, account_id)
    return _filter_week_comms(docs, week_monday=week_monday, report_friday=report_friday)


def fetch_week_client_comms(
    client: FairwindClient,
    account_id: str,
    *,
    week_monday: date,
    report_friday: date,
    reuse: bool = True,
) -> tuple[list[Document], str]:
    """External client emails + transcripts for Mon–Fri.

    Returns (docs, source) with source fairwind|fairwind-cached.
    """
    if reuse:
        cached = load_week_client_comms_cache(
            client,
            account_id,
            week_monday=week_monday,
            report_friday=report_friday,
        )
        if cached is not None:
            return cached, "fairwind-cached"

    export_id = _create_export_with_retry(
        client,
        account_id,
        week_monday,
        report_friday,
        data_types=["emails_external", "transcripts"],
    )
    if not export_id:
        # Persist empty marker so reuse doesn't keep re-hitting Fairwind for
        # accounts with no week emails.
        root = _comms_store_root(client, account_id, week_monday)
        root.mkdir(parents=True, exist_ok=True)
        (root / ".empty").write_text(report_friday.isoformat())
        return [], "fairwind"

    client.poll_export(export_id, interval_s=2)
    blob = client.download_zip(export_id)
    root = _comms_store_root(client, account_id, week_monday)
    root.mkdir(parents=True, exist_ok=True)
    empty_marker = root / ".empty"
    if empty_marker.exists():
        empty_marker.unlink()
    (root / "export.zip").write_bytes(blob)
    files_root = root / "files"
    if files_root.exists():
        shutil.rmtree(files_root, ignore_errors=True)
    files_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(files_root)
    docs = parse_export_dir(files_root, account_id)
    return (
        _filter_week_comms(docs, week_monday=week_monday, report_friday=report_friday),
        "fairwind",
    )


def format_comms_excerpt(docs: list[Document], *, limit: int = 12) -> str:
    if not docs:
        return "(none — no external emails or transcripts in Fairwind for this week)"
    parts: list[str] = []
    for d in docs[:limit]:
        src = d.source
        title = (d.title or "").strip()
        when = d.event_date.isoformat() if d.event_date else "?"
        body = (d.body or "").strip().replace("\n", " ")[:600]
        parts.append(f"[{src} {when}] {title}\n{body}")
    extra = len(docs) - limit
    if extra > 0:
        parts.append(f"… +{extra} more")
    return "\n\n---\n\n".join(parts)
