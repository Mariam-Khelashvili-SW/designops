"""Gmail adapter (§1 fallback). Built, switchable, dormant. If Fairwind coverage is
chronically short, flip the per-pipeline source_mode to `gmail`/`both` without a
rewrite (§1). Not wired in v1 — the §10.3 gate decides whether it is ever needed.
"""

from __future__ import annotations

from datetime import date

from designops.adapters.documents import Document


def list_messages(query: str, after: date, before: date) -> list[Document]:
    raise NotImplementedError(
        "Gmail source is dormant in v1 (source_mode=fairwind). Flip source_mode and "
        "wire this only if the §10.3 gate fails or Fairwind coverage is chronically short."
    )
