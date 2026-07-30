# Handoff — Design Ops A1 Daily Ops Digest

A FastAPI + Postgres app that generates Olga Kimalana's daily design-ops digest from the
UX/UI team's daily reports (pulled from Fairwind exports), cross-checked against Jira and
client comms, and emails it on a schedule. Server-rendered (Jinja2), no SPA.

Read next, in order: **`README.md`** (run locally), **`DEPLOY.md`** (hosting), this file
(current live state), then `CLAUDE.md` / `docs/` (product spec & rules).

---

## Live deployment (as of handoff)

- **Host:** Railway — project **designops-digest**
  (`https://railway.com/project/b99da406-b185-457c-a7e4-d181b7a636d7`)
- **App URL:** https://designops-app-production.up.railway.app  → `/daily-report`
- **Services:** `designops-app` (this repo, built from the `Dockerfile`) + **Postgres 18**
- **Deploy method:** `railway up` uploads the working dir and builds the Dockerfile.
  There is **no GitHub remote yet** — pushing to GitHub and connecting it to the Railway
  service would enable auto-deploy on push (recommended next step).
- **Data:** the Railway Postgres was seeded by copying the developer's local DB
  (`deploy_data_migrate.sh`): 1,326 accounts synced from the Fairwind Directory, 17 enabled
  for the digest, 10 roster people, 18 projects. Migrations run automatically on each boot
  (`alembic upgrade head` in the Docker CMD).

### Environment variables (set in Railway → designops-app → Variables, NOT in code)
`DATABASE_URL` (ref to the Postgres plugin), `FW_BASE_URL`, `FW_RESOURCE`, `FW_CLIENT_ID`,
`FW_CLIENT_SECRET`, `FW_DATA_TYPES`, `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `TIMEZONE`, `SETUP_OWNER_EMAIL`.
See `.env.example` for the full list + local defaults. Secrets live only in Railway and the
developer's local `.env` — neither is in git.

> ⚠️ **Rotate these three secrets** — they were shared over chat during setup and should be
> regenerated at their source, then updated in Railway: `FW_CLIENT_SECRET`,
> `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_SECRET`. (`*_CLIENT_ID` values are public, no rotation.)

---

## Open items (what's left to finish)

1. **Google OAuth for email sending.** Add the prod redirect URI
   `https://designops-app-production.up.railway.app/oauth/google/callback` to the OAuth
   client in Google Cloud Console (APIs & Services → Credentials), then click **Connect
   Google** in the app. The refresh token persists in Postgres (`app_state` table), so it
   survives redeploys. Until connected, the digest can be **generated** but not **emailed**.
2. **Schedule + recipients.** On `/daily-report`, the "Daily schedule" card sets the cron
   time, days, recipient list, and Delivery mode. Keep Delivery = **Generate only** until a
   few live runs have been eyeballed; switch to **Email recipients** to go live.
3. **Verify a generation run** end to end on prod (needs Fairwind + Anthropic keys, ~$0.30–
   0.60/run in Anthropic tokens).

---

## Architecture (the one rule that matters)

**Scope is code, judgement is prompt.** Who/what/when filtering is deterministic at ingest
(`designops/pipelines/filter.py`); only classification (blocker vs escalation) and phrasing
go to the LLM (`designops/pipelines/synthesis.py` + `designops/skills/daily-ops-digest.md`).
Because model attribution isn't 100%, every synthesis pass is followed by **code guards**
(`_prune_ungrounded`, `_enforce_structure`, `_flag_untracked_projects`,
`_reconcile_availability` in `daily_digest.py`) that drop anything not grounded in the corpus.

- `designops/adapters/fairwind.py` — Fairwind REST (OAuth2), zip export download + parser
- `designops/adapters/google_oauth.py` — Gmail send via OAuth (token in Postgres)
- `designops/api/` — FastAPI routes, Jinja templates, APScheduler (`scheduler.py`)
- `designops/core/` — config, DB models, identity/roster, project registry
- `scripts/` — `sync_accounts` (Directory → DB), `enable_accounts`, `seed`, `run_export`
- `tests/` — 19 pass + 1 skipped: `pytest -q`

## Common commands

```bash
# local dev
docker compose up -d db                 # Postgres on host :5435
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                     # then fill in secrets
alembic upgrade head && python -m scripts.seed
uvicorn designops.api.main:app --port 8077 --reload

# tests
pytest -q

# redeploy to Railway (after `railway login` + `railway link`)
railway up --service designops-app

# re-copy local DB → Railway Postgres (destructive on prod public schema)
bash deploy_data_migrate.sh
```
