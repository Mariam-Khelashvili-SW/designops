"""Pipeline ABC (§4). Every pipeline is the same six-stage shape:

    ingest() -> filter() -> synthesize() -> render() -> deliver() -> log()

ingest and filter are deterministic (scope is code); synthesize is the one LLM call
(judgement is prompt); render is code so the locked layout cannot drift; deliver is
gated by go_live; log writes the full audit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from designops.adapters.documents import Document
from designops.pipelines.filter import FilterResult


@dataclass
class PipelineContext:
    report_date: date
    ingest_batch_id: str | None = None
    reuse_ingest: bool = True
    send_mode_override: str | None = None
    documents: list[Document] = field(default_factory=list)
    filtered: FilterResult | None = None
    digest_json: dict | None = None
    html: str | None = None
    meta: dict = field(default_factory=dict)


class Pipeline(ABC):
    key: str

    @abstractmethod
    def ingest(self, ctx: PipelineContext) -> None:
        """Populate ctx.documents (union of sources, deduped by (source, external_id))."""

    @abstractmethod
    def filter(self, ctx: PipelineContext) -> None:
        """Deterministic scope filter; populate ctx.filtered (no LLM)."""

    @abstractmethod
    def synthesize(self, ctx: PipelineContext) -> None:
        """One LLM call; populate ctx.digest_json (structured)."""

    @abstractmethod
    def render(self, ctx: PipelineContext) -> None:
        """Render ctx.digest_json to ctx.html via the locked Jinja template."""

    @abstractmethod
    def deliver(self, ctx: PipelineContext) -> None:
        """Deliver per send_mode, only if go_live (gate lives in the delivery adapter)."""

    @abstractmethod
    def log(self, ctx: PipelineContext) -> None:
        """Write pipeline_run + run_document + artifact + flag rows."""

    def run(self, ctx: PipelineContext) -> PipelineContext:
        self.ingest(ctx)
        self.filter(ctx)
        self.synthesize(ctx)
        self.render(ctx)
        self.deliver(ctx)
        self.log(ctx)
        return ctx
