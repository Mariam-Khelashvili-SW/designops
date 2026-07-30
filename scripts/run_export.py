"""Manual export job — the daily-corpus fan-out (§6.1, §11.3, §12.2). No scheduler.

For a report_date, fan out one Fairwind export per ENABLED account (bounded
concurrency, 409 → reuse/backoff), union the documents, persist a reusable corpus, and
record an `ingest_batch`. Pulls **jira + transcripts only** — no Fairwind emails; the
internal team's dailies come from within the app. Synthesis (`run_daily_digest`) reads
the persisted corpus, so you ingest once and synthesize N times.

Runs on demand only:
  python -m scripts.run_export                          # previous working day
  python -m scripts.run_export --report-date 2026-07-17
  python -m scripts.run_export --report-date 2026-07-17 --fresh      # re-pull, ignore cache
  python -m scripts.run_export --accounts Northerner Felco           # override allowlist
  python -m scripts.run_export --data-types jira transcripts emails_external
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta

from designops.adapters.fairwind import (
    FairwindClient,
    corpus_file,
    load_corpus,
    save_corpus,
)
from designops.core.config import get_settings
from designops.core.db import session_scope
from designops.core.models import Account, IngestBatch


def _previous_working_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _resolve_accounts(tokens: list[str] | None) -> list[tuple[str, str]]:
    """Return [(fairwind_account_id, name)] for the allowlist or an explicit override."""
    with session_scope() as s:
        if tokens:
            out: list[tuple[str, str]] = []
            for tok in tokens:
                a = (
                    s.query(Account).filter_by(fairwind_account_id=tok).one_or_none()
                    or s.query(Account).filter(Account.name.ilike(tok)).one_or_none()
                )
                if a is None:
                    print(f"  ⚠ account not found, skipping: {tok!r}")
                    continue
                out.append((a.fairwind_account_id, a.name))
            return out
        rows = (
            s.query(Account)
            .filter_by(digest_enabled=True)
            .order_by(Account.name)
            .all()
        )
        return [(a.fairwind_account_id, a.name) for a in rows]


def run_export(
    report_date: date,
    *,
    account_tokens: list[str] | None = None,
    data_types: list[str] | None = None,
    fresh: bool = False,
    concurrency: int = 3,
) -> dict:
    settings = get_settings()
    if not settings.fairwind_configured:
        raise SystemExit("FW_CLIENT_ID / FW_CLIENT_SECRET not set — load them from env (§10).")

    if not fresh and load_corpus(settings, report_date) is not None:
        print(
            f"↩ corpus for {report_date} already exists at {corpus_file(settings, report_date)}.\n"
            "  Reusing it (pass --fresh to re-pull)."
        )
        return {"reused": True, "report_date": report_date.isoformat()}

    accounts = _resolve_accounts(account_tokens)
    if not accounts:
        raise SystemExit(
            "No accounts to export. Enable some with `python -m scripts.enable_accounts` "
            "or pass --accounts."
        )
    account_ids = [aid for aid, _ in accounts]
    names = {aid: n for aid, n in accounts}
    types = data_types if data_types is not None else settings.fw_data_types

    print(
        f"→ exporting {len(account_ids)} account(s) for {report_date} "
        f" (types: {', '.join(types)})"
    )
    started = datetime.now(UTC)
    client = FairwindClient(settings)
    documents, coverage = client.prepare_corpus(
        account_ids, report_date, concurrency=concurrency, data_types=types
    )
    finished = datetime.now(UTC)

    corpus_path = save_corpus(settings, report_date, documents)
    status = "flagged" if coverage.get("exports_failed", 0) else "ok"

    with session_scope() as s:
        batch = IngestBatch(
            report_date=report_date,
            account_ids=account_ids,
            started_at=started,
            finished_at=finished,
            status=status,
            doc_count=len(documents),
            coverage={**coverage, "names": names},
        )
        s.add(batch)
        s.flush()
        batch_id = str(batch.id)

    # --- report ---
    per = coverage.get("docs_per_account", {})
    print(f"\n  corpus:      {corpus_path}  ({len(documents)} documents)")
    print(f"  ingest_batch {batch_id}  status={status}")
    print(f"  accounts:    {coverage['exports_succeeded']} ok / "
          f"{coverage['exports_failed']} failed of {coverage['accounts_requested']}")
    if coverage.get("failed_accounts"):
        print(f"  ⚠ failed:    {', '.join(names.get(a, a) for a in coverage['failed_accounts'])}")
    print("  per account:")
    for aid in account_ids:
        print(f"    {names.get(aid, aid)[:34]:34} {per.get(aid, 0):>4} docs")
    by_source: dict[str, int] = {}
    for d in documents:
        by_source[d.source] = by_source.get(d.source, 0) + 1
    print(f"  by source:   {by_source or '—'}")
    if status == "flagged":
        print("  ⚠ status flagged — an export failed; coverage is incomplete (§11.1).")

    return {
        "reused": False,
        "report_date": report_date.isoformat(),
        "ingest_batch_id": batch_id,
        "status": status,
        "documents": len(documents),
        "coverage": coverage,
        "corpus_path": str(corpus_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual Fairwind export fan-out (no scheduler).")
    ap.add_argument("--report-date", help="YYYY-MM-DD (default: previous working day)")
    ap.add_argument(
        "--accounts", nargs="*", help="account ids or names (default: enabled allowlist)"
    )
    ap.add_argument("--data-types", nargs="*", help="override FW_DATA_TYPES for this run")
    ap.add_argument("--fresh", action="store_true", help="re-pull even if a corpus exists")
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    report_date = (
        date.fromisoformat(a.report_date)
        if a.report_date
        else _previous_working_day(datetime.now(UTC).date())
    )
    run_export(
        report_date,
        account_tokens=a.accounts,
        data_types=a.data_types,
        fresh=a.fresh,
        concurrency=a.concurrency,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
