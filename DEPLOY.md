# Deploy — Design Ops daily digest (Railway)

The app is an always-on container: FastAPI + an in-process scheduler that fires the daily
digest and emails it. It needs a **persistent process** and a **managed Postgres** — which
is why Vercel (serverless) doesn't fit and Railway/Render/Fly do. This guide uses
**Railway**; Render/Fly are the same idea with different dashboards.

**~20–30 min.** You'll need: a Railway account, this repo on GitHub (or the Railway CLI),
your Fairwind + Anthropic + Google OAuth credentials, and Postgres client tools
(`pg_dump`/`pg_restore`, or Docker) to copy your local data up.

---

## 1. Create the project + Postgres
1. railway.app → **New Project** → **Deploy from GitHub repo** (or `railway init` with the CLI). Railway auto-detects the `Dockerfile`.
2. In the project, **+ New → Database → Postgres**. Railway sets a `DATABASE_URL` you can reference from the app service.

## 2. Set environment variables (app service → Variables)
Rotate the three secrets that passed through chat when you set them here.

| Var | Value |
|---|---|
| `DATABASE_URL` | reference the Postgres plugin's URL (Railway: `${{Postgres.DATABASE_URL}}`) |
| `FW_CLIENT_ID` / `FW_CLIENT_SECRET` | Fairwind OAuth client (**rotate the secret**) |
| `FW_BASE_URL` | `https://fairwind.scandiweb.com` |
| `FW_RESOURCE` | `https://fairwind.scandiweb.com/api/v1` |
| `ANTHROPIC_API_KEY` | Anthropic key (**rotate**) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth client (**rotate the secret**) |
| `PUBLIC_APP_URL` | `https://<your-app>.up.railway.app` (used to build the OAuth callback) |
| `GOOGLE_REDIRECT_URI` | `https://<your-app>.up.railway.app/oauth/google/callback` (or omit and let the app derive it from `PUBLIC_APP_URL`) |
| `TIMEZONE` | `Europe/Riga` |
| `SETUP_OWNER_EMAIL` | your email |
| `FW_DATA_TYPES` | `emails_internal,emails_external,jira,transcripts` |

(Get `<your-app>.up.railway.app` from the service's **Settings → Networking → Generate Domain**.)

## 3. Copy your local data up (so accounts/projects/roster/leave carry over)
The prod DB starts empty. Your local dev DB holds all the state you built (1,326 synced
accounts, enabled set, projects incl. Felco/Universal, roster + Mariam's leave, links).
A full dump→restore is the simplest way to preserve it — it includes the schema **and**
`alembic_version`, so the app's `alembic upgrade head` on boot becomes a no-op.

```bash
# 1) dump the local DB (Docker Postgres on :5435)
docker exec designops-db-1 pg_dump -U designops -Fc designops > designops.dump

# 2) restore into the Railway Postgres (grab its PUBLIC connection string from
#    Railway → Postgres → Connect → "Postgres Connection URL")
pg_restore --no-owner --no-privileges --clean --if-exists \
  -d "postgresql://…railway public url…" designops.dump
```
Do this **before** (or right after creating) the app service; `--clean --if-exists` makes it
safe to re-run. Your Google token is in `app_state` and comes across too — but you'll
re-connect in step 5 anyway because the redirect URI changes.

> Prefer a clean slate instead? Skip the dump; the app auto-runs migrations and inserts
> the three pipeline rows on boot. Then in a Railway shell run `python -m scripts.seed`
> (roster + projects), `python -m scripts.sync_accounts`, `python -m scripts.enable_accounts`
> — but you'd lose the manual project/leave tweaks.

## 4. Deploy
Trigger a deploy (push to the branch, or Railway **Deploy**). On boot the container runs
`alembic upgrade head` then starts uvicorn + the scheduler. Open the generated domain —
you should see the Daily report page.

## 5. Point Google OAuth at production
1. Google Cloud Console → **APIs & Services → Credentials → your OAuth client → Authorized redirect URIs → Add** `https://<your-app>.up.railway.app/oauth/google/callback` (must match `GOOGLE_REDIRECT_URI` exactly). Save.
2. In the deployed app → Daily report → **Delivery → Connect Google** → authorize. The refresh token is stored in Postgres (`app_state`), so it survives redeploys.

## 6. Turn on the schedule
Daily report → **Daily schedule** card: set the **time** (e.g. 12:00), **Days**, add
**Recipients**, set **Delivery = Email recipients**, tick **On**, **Save**. The card shows
the next run. Confirm with a manual **Generate** first; the **Run log** shows exactly what
was pulled.

---

## Notes
- **Corpus store** — `CORPUS_STORE_DIR=/data/corpus` in the image. Without a mounted volume
  it's ephemeral (each run re-pulls from Fairwind — fine for once a day). Attach a Railway
  **Volume** at `/data` if you want it cached.
- **Always-on** — the in-process scheduler only fires while the container runs. Railway
  keeps it up; just don't scale the service to 0. (For firing independent of the app being
  up, the upgrade path is an external cron hitting an endpoint, or Temporal — not needed now.)
- **Cost** — roughly a few USD/month (small service + Postgres) plus the per-run Anthropic
  tokens (~$0.30–0.60 each).
- **`go_live`** — the delivery adapter's separate hard gate is bypassed by the app's own
  "Email recipients" send path; the real gate is the schedule card's **On** + **Delivery**
  + recipients. Keep Delivery = "Generate only" until you've eyeballed a few live runs.
