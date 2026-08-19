# Deploying moot

## What has to run

Three things, always:

- **Web** — a long-running Python HTTP process serving both the API and the
  built React bundle at `/app/*`. FastAPI with uvicorn.
- **Worker** — a long-running Python process that drains the `jobs` table:
  Canvas syncs and indexing. Without it, notes save but never become
  searchable.
- **Postgres** — with the `pgvector` extension available. SQLite works
  locally and in the tests but does not survive a redeploy on any hosted
  platform, and it cannot be shared between web and worker.

That combination — long-running processes plus a stateful database —
rules out the serverless platforms (Vercel, Netlify, Cloudflare Workers).
Their function model kills the worker between invocations and expects the
database to be somebody else's problem, which for a real app it eventually
is. Pick a platform that runs containers or long-lived processes and lets
you attach Postgres.

The two straightforward choices are **Fly.io** and **Railway**. This repo
carries a config for each. Any container platform works — Render, Google
Cloud Run with a VPC connector, a plain VM behind a reverse proxy — the
Dockerfile is portable.

## Configuration

Everything is via environment variables. Nothing is hardcoded and no
secret is written to the database or logged. `.env.example` is the full
list; the ones that matter for production are:

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql+psycopg2://…`. SQLite silently loses data on redeploy. |
| `STUDYLINK_SECRET_KEY` | yes | Encrypts stored Canvas tokens. Generate with the command below. The all-zero CI key is public — preflight rejects it in production. |
| `STUDYLINK_ENV` | yes | Set to `production`. Turns preflight warnings into fatal errors and enables secure-cookie defaults. |
| `ANTHROPIC_API_KEY` | no | Chat, agent, and LLM judge stop working without it (they return 503); retrieval and metrics do not need it. |
| `LLM_MONTHLY_BUDGET_USD` | no | Per-user 30-day cap. `0` disables the cap — one user with a script can then spend without limit. |
| `CORS_ALLOW_ORIGINS` | no | Comma-separated origins. Empty means CORS off, which is correct when the frontend is served by the same process. `*` is refused in production. |
| `TRUST_PROXY_HEADERS` | no | Only set behind a proxy that overwrites `X-Forwarded-For`. Otherwise it is a rate-limit bypass. |
| `EMBEDDING_PROVIDER` | no | `hash` (default) / `voyage` / `sentence-transformers`. |

Generate a secret key locally, once, and set it as a secret on the
platform:

```
python -c 'from studylink.vault import generate_key; print(generate_key())'
```

Use that command rather than an equivalent-looking one-liner. The
encoding is **standard base64 with padding**, so a real key ends in `=`.
The obvious alternative — `base64.urlsafe_b64encode(...).rstrip("=")` —
produces a string that looks identical at a glance and is rejected by the
decoder. Preflight now refuses to boot on a key it cannot parse, which is
the cheapest place to find out.

Losing this key strands every Canvas token in the database — they are
still there but cannot be decrypted. See `docs/SECRETS.md` for rotation.

## What preflight checks at boot

`studylink/preflight.py` runs when the API module is imported and refuses
to start with a broken production config. It catches the failures that
are otherwise silent:

- `DATABASE_URL` unset (would fall back to a SQLite file on the container).
- `DATABASE_URL` pointing at SQLite (single file, cannot be shared with
  the worker, deleted on redeploy).
- `STUDYLINK_SECRET_KEY` unset or the public CI key.
- `CORS_ALLOW_ORIGINS=*` (lets any page on the internet call the API with
  a signed-in user's token).

Warnings-only in development, fatal in production. The platform's logs
show the specific message.

## Health probes

- `GET /healthz` — liveness. Touches nothing. `200 {"ok": true}` whenever
  the process is up. Point the platform's liveness/restart probe at this
  one; if it queried the database, a brief outage would restart every web
  container and turn a hiccup into a restart loop.
- `GET /readyz` — readiness. Opens its own database connection and
  reports whether migrations are at the expected revision. `200` when
  everything answers, `503` with a body describing what did not. Point the
  load balancer's traffic-eligibility probe at this one so it drains a
  broken container instead of sending users to it.

Both are unauthenticated (a load balancer has no credentials) and report
only whether dependencies answer — never anything about user data.

## Fly.io

`fly.toml` in the repo root. Fly runs one image with two `[processes]`
entries — `web` and `worker` — which is the split we want without a
second image to build.

```bash
brew install flyctl                   # or the platform equivalent
fly auth login
fly launch --no-deploy --copy-config  # accept the existing fly.toml
fly postgres create --name studylink-db
fly postgres attach studylink-db      # sets DATABASE_URL in secrets
fly secrets set \
  STUDYLINK_SECRET_KEY="$(python -c 'from studylink.vault import generate_key; print(generate_key())')" \
  ANTHROPIC_API_KEY=sk-ant-... \
  STUDYLINK_ENV=production
fly deploy
```

`fly.toml` sets `release_command = "alembic upgrade head"`, so migrations
run once per deploy, before web or worker starts. If migrations fail the
deploy aborts before any process rolls out.

Only `web` gets a public port; the worker has no listener and is not
routable from the internet.

Scale each process independently:

```bash
fly scale count web=2 worker=1
fly logs -a studylink
```

## Railway

`railway.toml` and `Procfile` are both in the repo root. Railway's model
is one service per process, sharing the same repo, so you create two:

1. **New project → Deploy from GitHub repo.**
2. **Add Postgres plugin.** Railway sets `DATABASE_URL` on every service
   linked to it. Add the `pgvector` extension from the plugin's UI:
   `CREATE EXTENSION IF NOT EXISTS vector;`
3. **`web` service:** default start command
   (`alembic upgrade head && uvicorn studylink.api:api --host 0.0.0.0 --port $PORT`)
   is what railway.toml ships. Expose port 8000, generate a public domain.
4. **`worker` service:** same repo, second service. Override
   customStartCommand to `python scripts/run_worker.py`. No public domain.
5. **Shared secrets** on both services: `STUDYLINK_SECRET_KEY`,
   `ANTHROPIC_API_KEY`, `STUDYLINK_ENV=production`.

The `release` command in `Procfile` handles migrations if you use
Railway's nixpacks builder instead of the Dockerfile.

## Render

Render builds the `Dockerfile` directly. Three things about it differ from
the other two platforms enough to be worth stating.

**The health check must point at `/healthz`.** Render defaults to `/`, and
`/` here redirects to `/app/`. A 307 is not a 2xx, so the check fails, and
a failing check means Render will not route traffic to a container that is
in fact serving fine. The symptom is a working app behind a "Not Found"
page. Set **Settings → Health Check Path** to `/healthz`.

**There is no release phase below the paid tier**, so migrations cannot run
between build and boot. That is why `docker-entrypoint.sh` runs
`scripts/migrate.py` before handing off to the real process: whichever
container starts first takes a Postgres advisory lock and migrates, the
rest wait and then carry on. Without it, the schema never gets created and
the first signup returns 500 from a missing `users` table.

**The worker is a separate paid service.** Render's free tier has web
services only; a Background Worker is the cheapest paid tier. Skip it and
everything still runs, but sync and indexing queue up and never execute —
which reads as a bug rather than a missing process.

You do not have to do anything for this to work: the image defaults
`RUN_WORKER=1`, so a single web container drains its own queue. Two
consequences worth accepting deliberately — background work competes with
request handling for the same CPU, so a large Canvas sync slows the site
while it runs; and a free web service that sleeps when idle takes its
worker with it, so jobs progress while someone is using the app rather
than on a schedule. Fine for a demo, not for real load.

When you do add a Background Worker service, set `RUN_WORKER=0` on the web
service so only one process is draining the queue. (`fly.toml` and
`docker-compose.yml` already do this, since both define a real worker.)

Setup:

1. **New → Postgres.** Copy the *Internal Database URL* — it is
   `postgres://…`, which the app rewrites to the driver form on load.
2. **New → Web Service**, point it at the repo, Runtime **Docker**.
3. Environment: `DATABASE_URL` (the internal URL), `STUDYLINK_SECRET_KEY`,
   `STUDYLINK_ENV=production`, optionally `ANTHROPIC_API_KEY`.
4. **Health Check Path:** `/healthz`.
5. Connect with `psql` using the *External* URL and run
   `CREATE EXTENSION IF NOT EXISTS vector;`.
6. Optional: **New → Background Worker**, same repo, start command
   `python scripts/run_worker.py`, same environment.

Two free-tier behaviours to expect: web services sleep after about 15
minutes idle and take roughly a minute to answer the next request, and
free Postgres instances expire after 30 days.

## Local containers (docker-compose)

Not for production, but proves web and worker actually run in separate
processes against Postgres before a deploy proves it expensively.

```bash
docker compose up --build
docker compose run --rm web python scripts/seed_demo.py --account demo@school.edu
open http://127.0.0.1:8000
```

`compose` starts four things: Postgres, a one-shot `migrate` container
that runs `alembic upgrade head` and exits, then `web` and `worker` which
both `depends_on: migrate: service_completed_successfully` — so there is
no race where two web replicas try to migrate at once.

## First-deploy checklist

- [ ] Postgres reachable from web and worker (same URL in both).
- [ ] `pgvector` extension created in the database.
- [ ] `STUDYLINK_SECRET_KEY` generated and set (not the CI key).
- [ ] `STUDYLINK_ENV=production` set on both services.
- [ ] Migrations ran successfully (`alembic upgrade head`).
- [ ] `curl https://<host>/healthz` returns `{"ok": true}`.
- [ ] `curl https://<host>/readyz` returns `{"ok": true, ...}`.
- [ ] `/app/` loads the React bundle (not the FastAPI 404 page).
- [ ] Signup works; the new account can create a note and see it indexed
      within a few seconds — that proves the worker is running and can
      write vectors.

## What is deliberately not automated

- **Backups.** The platform's Postgres plugin does daily snapshots; take
  a first manual one before enabling public signup and confirm you know
  how to restore.
- **Key rotation.** `docs/SECRETS.md` covers it. The rotation format is
  `v2:<new>,v1:<old>` — new keys write, both keys read, and old
  ciphertext can be re-encrypted with `scripts/rotate_vault_key.py`.
- **Log shipping.** Each platform's default log view is enough for
  bring-up; wire up a real destination when you have one.
- **Custom domain and TLS.** Both platforms terminate TLS on their edge
  and hand you a subdomain; add your own domain in the platform's UI
  when you have one to add.
