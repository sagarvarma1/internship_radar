# Internship Radar

Watches the public job boards of ~200 tech companies, spots new internship postings within minutes,
filters them to roles that fit you, and alerts you by email and text with a direct apply link. A
small web app lists every open internship it tracks, with per-posting status (interested / applied /
skipped) and a board-health page.

Everything shown comes from live, unauthenticated ATS APIs that companies publish for their own
career pages:

| ATS | Endpoint |
| --- | --- |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{board}` |
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `GET https://api.lever.co/v0/postings/{site}?mode=json` (EU: `api.eu.lever.co`) |

It never scrapes LinkedIn, Indeed, or company career sites. Companies that don't use one of these
three ATSs are listed as **unsupported**.

## How it works

```
registry.yaml ──► poller ──► ATS APIs (ETag, 1 req/s per host, backoff)
                    │
                    ▼
            parse → normalize → evaluate (internship? fits? score + reasons)
                    │
                    ▼
            SQLite: diff against stored postings
              new + matching ─► alert queue ─► email / SMS / Discord / Slack
              missing        ─► marked closed
                    │
                    ▼
            web app: list, filters, status, health
```

* **First poll of a board is a baseline**: everything is stored, nothing is alerted. After that,
  only postings that appear later trigger alerts.
* **Dedupe**: postings are keyed by `{ats}:{board}:{source id}`; deliveries are recorded per
  `(posting, channel)`, so nothing is ever sent twice, and a failing channel retries next run
  without re-sending to channels that worked.
* **Batching**: up to `batch_threshold` alerts go out individually; more than that become one digest.
* **Politeness**: descriptive User-Agent, `If-None-Match` conditional requests (Ashby, Greenhouse and
  Lever all return ETags; unchanged boards cost a 304), at most one request per second per host,
  exponential backoff with jitter, and boards that keep failing are polled less often (up to 6h).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/sagarvarma1/internship-radar && cd internship-radar
uv sync
uv run radar poll            # first run: baseline of every board (~3 min)
uv run radar serve           # http://localhost:8000
uv run pytest                # 68 tests, offline
```

Run `radar poll` every ~10 minutes (cron, or Orbit's scheduled job; see [DEPLOY.md](DEPLOY.md)),
or run `radar worker` to poll forever with each board spread across the interval.

### Commands

| Command | What it does |
| --- | --- |
| `radar poll [--spread SECS] [--only NAME…] [--no-alerts]` | Poll every board once, then send alerts |
| `radar worker` | Poll forever on `poll.interval_minutes` |
| `radar serve [--port 8000]` | Web app |
| `radar discover <careers-url or name> [--add --name N --tags …]` | Find a company's ATS board |
| `radar validate` | Fetch every registry board and report broken ones |
| `radar report [--markdown]` | Coverage report |
| `radar rescore` | Re-apply filters to stored postings after editing `config.yaml` |
| `radar test-alert email\|sms\|discord\|slack\|console` | Send the current top match as a test |

## Adding companies

```sh
# From a careers page: detects Ashby / Greenhouse / Lever embeds and links, validates the slug live
uv run radar discover https://www.notion.com/careers --add --name Notion --tags product
# From a name: tries likely slugs on all three ATSs
uv run radar discover "Black Forest Labs"
```

`discover` prints each candidate with its live job count (and, for Greenhouse, the company name the
board reports). **Check the match before adding**: short slugs collide. While seeding this registry,
`ashby:warp` turned out to be an HR startup rather than Warp the terminal, and `greenhouse:lovable` an
Italian clothing brand. If no supported board is found, `--add` records the company as unsupported,
noting any other ATS it saw (Workday, SmartRecruiters, …).

Or edit `registry.yaml` directly:

```yaml
- name: Notion
  ats: ashby            # ashby | greenhouse | lever | unsupported
  slug: notion
  careers_url: https://www.notion.com/careers
  tags: [target, product]
```

Then run `radar validate`.

## Configuring filters

`config.yaml` → `filters` (run `radar rescore` after changes):

| Key | Default | Meaning |
| --- | --- | --- |
| `role_types` | `[swe, ml, research, applied_science]` | Also: `data`, `hardware`. `[]` = any role |
| `countries` | `[US]` | `[]` = anywhere |
| `allow_remote` | `true` | Fully-remote roles pass but score below on-site/hybrid |
| `allow_unknown_location` | `true` | Keep postings with no parseable location |
| `preferred_cities` | `[]` | +5 when a location matches |
| `graduation_years` | `[]` | e.g. `[2028]`. Postings that state a graduation window excluding these fail |
| `degree_levels` | `[bs, ms]` | Postings limited to other levels ("MS/PhD" passes for ms, "PhD Research Intern" or "MBA" fails) |
| `preferred_tags` | `[target, ai-lab]` | Registry tags worth +5 |
| `min_score` | `0` | |

**Internship detection** uses, in order: the title (`intern`, `internship`, `co-op`, `student
researcher`, `working student`, and season+year titles such as "FPGA Associate (Summer 2027)"), the
ATS employment type (Ashby `Intern`, Lever commitment `Internship`, Greenhouse metadata), and only
then the description, which counts only with an internship-specific phrase *and* a student or
seasonal title. Word boundaries keep out "Internal Tools", "International", "Database Internals";
titles about interns ("University Recruiter, Interns", "Internship Program Manager") and new-grad
roles ("Summer 2027 Graduates") are excluded. See `tests/test_filters.py` for the cases.

**Score** (shown with reasons on every posting and alert): internship 40, best matching role up to
+25, target country +10, on-site/hybrid +5, preferred city +5, graduation year fits +5, preferred
company tag +5, posted in the last 3 days +5.

## Configuring alerts

Choose channels in `config.yaml` (`alerts.channels`) or with `RADAR_ALERT_CHANNELS=email,sms`.
Secrets and recipients come from environment variables only:

| Channel | Environment |
| --- | --- |
| email (SMTP) | `SMTP_HOST`, `SMTP_PORT` (587 STARTTLS / 465 SSL), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `RADAR_EMAIL_TO` |
| email (Resend) | `RESEND_API_KEY`, `EMAIL_FROM`, `RADAR_EMAIL_TO` |
| sms (Twilio) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `RADAR_SMS_TO` (E.164, e.g. `+14155550123`) |
| discord | `DISCORD_WEBHOOK_URL` |
| slack | `SLACK_WEBHOOK_URL` |

Optional: `RADAR_PUBLIC_URL` (linked from digests). SMS is held, not dropped, during
`sms_quiet_hours`. Verify a channel with `radar test-alert email`.

For Gmail SMTP, use an [app password](https://support.google.com/accounts/answer/185833)
(`SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`). Twilio needs a sending number; US numbers must
complete A2P 10DLC or toll-free verification before messages are delivered.

## Web app

* `/`: open internships matching your filters, best match first. Filter by company tag, role,
  workplace, status, location or text; "New only" shows postings first seen since your previous
  visit (a visit ends after 30 idle minutes). Status buttons save instantly.
* `/health`: boards tracked per ATS, per-board health (last success, consecutive failures, last
  error), unsupported companies, recent poll runs.
* `/api/postings`: same data as JSON. `/healthz`: liveness plus whether polling is stale.

Set `RADAR_PASSWORD` to put the whole app (except `/healthz`) behind HTTP basic auth (any
username). Do this for any public deployment, since statuses are writable.

## Deploying

See [DEPLOY.md](DEPLOY.md) (Orbit, Docker, anything with cron and a disk).

## Layout

```
src/internship_radar/
  models.py     shared interfaces: Company, Posting, Match, Alert
  ats.py        endpoint URLs + Ashby/Greenhouse/Lever parsers
  fetch.py      polite HTTP (ETag, per-host limit, backoff)
  discover.py   careers URL / name -> validated board
  filters.py    internship detection, role classification, fit filters, score
  db.py         SQLite schema, snapshot diffing, close tracking
  poller.py     poll cycle, staggering, failure backoff
  alerts.py     formatting, batching, dedupe, senders
  web.py        FastAPI app + templates/
  report.py     coverage report
registry.yaml   companies
config.yaml     filters, alerts, polling
```
