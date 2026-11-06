# Deploying

Radar needs three things from a host:

1. A long-running **web process** (`radar serve --port $PORT`).
2. A **scheduled job every minute** (`radar poll`: 25-50 s per run, hard-capped at ~50 s; a run
   skips itself if the previous one is still going). It makes ~15-30 outbound requests/second to
   the job-board APIs at peak.
3. A **persistent disk** shared by both, holding one SQLite file (`RADAR_DB`, WAL mode, so the web
   app and poller can use it at the same time). It holds user accounts, so back it up.

Also schedule `radar discover-all` weekly (or run it by hand and commit `discovered.yaml`).

And from you: an email sender. For a public site use [Resend](https://resend.com) with a domain
you control (add its DNS records so mail isn't flagged as spam), then set `RESEND_API_KEY` and
`EMAIL_FROM`. Set `RADAR_SITE_URL` to the public https URL.

## Orbit

`orbit.json` describes the deployment. Orbit (as of Nov 2026) supports Node/Next.js/static apps and
JS functions, but not yet Python, cron jobs or volumes. Those are being added, and this config is
written against them. This is the behaviour it assumes, so the Orbit side can be built to match:

| Key | Assumed behaviour |
| --- | --- |
| `runtime: "python"`, `pythonVersion` | Build image with that Python; `install` runs in the repo root |
| `web.command` | Long-running process; Orbit sets `$PORT` and routes the project domain to it |
| `web.healthcheck` | GET path that must return 200 before traffic switches to a new deploy |
| `crons[].schedule` | Standard 5-field cron, UTC |
| `crons[].command` | Runs in the same image, env and volumes as `web` |
| `crons[].timeoutSeconds` | Kill after this long (a poll takes ~35 s) |
| `crons[].concurrency: "forbid"` | Skip a tick if the previous run is still going |
| `volumes[]` | Persistent disk mounted into web *and* crons, surviving redeploys |
| `env` | Plain (non-secret) env vars; secrets set in the dashboard/API |

Steps once those land:

1. Create the project from this repo in the Orbit dashboard (GitHub App).
2. Set env/secrets: `RADAR_SITE_URL`, `EMAIL_FROM`, `RESEND_API_KEY` (or the SMTP variables).
3. Push to the production branch. The first cron run baselines every board without alerting.
4. Check `/healthz`: `poll_stale` should be `false` within a couple of minutes and `mail` should
   say `resend` or `smtp` (not `console`). Sign up yourself and send `radar test-email`.

### Rough edges noted while building against Orbit

From using Orbit as a user (landing page, README, `docs/self-hosting.md`):

* **Language support isn't stated up front.** The landing page says "Put a file in `/api` and it's an
  endpoint", but only JS/TS functions run. A Python user finds out after reading the README's
  framework detection rules.
* **No background work.** No cron, no workers, no queue. Any app that polls, sends digests or cleans
  up needs an external scheduler. This is the main blocker for Radar, which needs a job every
  minute (so sub-5-minute schedules matter, as does "skip if still running").
* **No persistent storage for apps.** Functions can't write to disk and there's no managed
  database, so even a tiny SQLite app has nowhere to keep state.
* **No Orbit-native config file.** Only `vercel.json` is read. A project needing a start command,
  port, cron or volume has no place to declare it (hence `orbit.json` here as a proposal).
* **No CLI**, so there's no `orbit deploy`, `orbit env set` or `orbit logs`. Env vars are dashboard/API
  only, and there are **no runtime logs** (build logs only), so a failing poller would be invisible.
* **Docs are one markdown file**: the "Docs" link goes to `docs/self-hosting.md` on GitHub.
  Getting-started, config reference, env vars and limits are spread across the README.
* **Inconsistencies**: the landing page says 2 GB RAM, the self-hosting doc says 4 GB + 2 vCPU;
  `functions.includeFiles` is on the landing page but not in the README's supported list.
* Cloud is "free during beta, up to 10 projects" with no word on what happens after beta.

## Docker (any host)

```sh
docker build -t internship-radar .
docker volume create radar
docker run -d --name radar-web -v radar:/data -p 8000:8000 --env-file .env internship-radar
# cron on the host, every minute:
* * * * * docker run --rm -v radar:/data --env-file /path/.env internship-radar radar poll
```

Or a single container that polls forever: `docker run -d -v radar:/data --env-file .env internship-radar radar worker`.
