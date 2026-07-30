# Design Ops Pipeline App — A1 Daily Ops Digest (v1)

A pipeline runner with a config store and a thin read UI:
**scheduler → ingest → deterministic filter → LLM synthesis → render → deliver → log.**

One pipeline in v1: the **A1 Daily Ops Digest** for Olga Kimalana (Head of Design).
Full spec: `design-ops-app-SPEC.md`.

## The one rule that shapes everything

> **Scope is code, judgement is prompt.** (§2)

Who/what/when (roster, project, date) is filtered **deterministically at ingest** — the model
never sees a non-design daily. Only blocker-vs-escalation classification, verbatim fidelity and
phrasing go to the model.

## Safety posture (v1)

- Ships `send_mode: none`, `go_live: false`. **Nothing is emailed to anyone.** The digest is
  generated in-app and read in the artifact viewer (§12). The `go_live` gate is enforced in the
  delivery adapter itself, not just the UI.
- Secrets (`FW_CLIENT_SECRET`, `ANTHROPIC_API_KEY`) come from env only — never the DB, never git.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in FW_* and ANTHROPIC_API_KEY when ready

docker compose up -d db         # Postgres 16 on host port 5435
alembic upgrade head            # create schema
python -m scripts.seed          # load roster + project registry from seeds/*.yaml
```

Run the fast, offline scope-filter tests (no DB, no network, no LLM):

```bash
pytest -m "not db and not llm and not fairwind"
```

## Before build step 1 — the §10.3 gate

Whether the designers' dailies actually live in Fairwind `emails_internal` is an **empirical
question** the spec insists you settle first. With `FW_CLIENT_ID`/`FW_CLIENT_SECRET` in env:

```bash
scripts/test_emails_internal.sh          # or: python -m scripts.test_emails_internal
```

Test subject is **Predrag Gavrilovikj** (§11.2) — the designer with zero Fairwind search hits, so a
pass on him is a real pass. If his 17 Jul daily does not appear with its per-project sections intact,
`source_mode` flips to `gmail` and the export path becomes registry-sync + weekly-sweep only.

## Layout

```
designops/
  core/        config (env), db, models (§5 + §11), enums
  adapters/    documents, fairwind (export), jira, gmail, llm, delivery
  pipelines/   base ABC, filter (pure, deterministic), daily_digest
  skills/      daily-ops-digest.md (versioned prompt) + templates/digest.html.j2
  api/         FastAPI read UI (pipelines, run log, artifact viewer, accounts, config)
  seeds/       roster.yaml, projects.yaml
  migrations/  Alembic
scripts/       seed.py, test_emails_internal
tests/         scope filters (pure), golden digest, fixtures/2026-07-17/
```

## Open items (from §9 — needed before go-live, not before build)

1. Olga's email + exact daily query/label.
2. Atlassian creds, or accept Fairwind `types:["jira"]` read-only.
3. **Confirm the roster of 9 + emails + Jira accountIds; confirm Agnese Čākure's status** — the
   seed derives `firstname.lastname@scandiweb.com` and marks every unconfirmed identity
   `identity_verified: false`.
4. Standing-blockers carry-forward decision (recommend: no in v1).
5. Run the §10.3 test.
6. **Rotate the leaked Fairwind API client secret** and load the new pair from env.
```
