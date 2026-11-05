# Internship Radar

Watches the public job boards of ~200 tech companies, spots new internship postings within minutes,
filters them to roles you're eligible for, and emails you a direct apply link. A
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
              new + matching ─► alert queue ─► email (or Discord / Slack)
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
git clone https://github.com/sagarvarma1/internship_radar && cd internship_radar
uv sync
uv run radar poll            # first run: baseline of every board (~3 min)
uv run radar serve           # http://localhost:8000
uv run pytest                # 131 tests, offline
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
| `radar test-alert email\|discord\|slack\|console` | Send the current top match as a test |

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

## Your profile: eligibility checks

Internship applications ask the same eligibility questions, and postings state the same
requirements. Your answers go in `profile.local.yaml` (git-ignored; same keys as `config.yaml` →
`profile`) or, when deployed, the `RADAR_PROFILE` env var as JSON, e.g.
`{"graduation": "2028-05", "degree_level": "bs", "gpa": 3.7}`. Each posting's stated requirements are
extracted and checked against them:

| Profile key | Example | What postings say (verbatim from live postings) | Check |
| --- | --- | --- | --- |
| `graduation` | `"2028-05"` | "graduating between September 2027 - July 2028", "by June 2028", "December 2027 or later", "Fall 2027 or Spring/Summer 2028" | inside the window (±1 month) |
| (derived) | | "Must be returning to school at the end of the internship", "at least one semester remaining" | you graduate after the internship term ends |
| (derived) | | "Currently a junior", "Rising seniors are preferred", "completed at least the second year" | your year in school, computed from `graduation` |
| `degree_level` | `bs` | "Enrolled in a graduate program (Master's or PhD)", titles like "MS/PhD" | your level is accepted |
| `majors` | `[computer science]` | "pursuing a BA/BS degree in Accounting, Finance, or a related field" | listed, or a "technical field" that covers it; "or related field" only warns unless `strict_majors: true` |
| `gpa` | `3.7` | "GPA of 3.0 or above", "3.5+ GPA preferred" | at least the minimum ("preferred" only warns) |
| `citizenship` | `us_citizen` / `permanent_resident` / `other` | "Must be a U.S. Person" (ITAR), "U.S. Citizenship, Lawful Permanent Residency, or Refugee/Asylee Status Required", "Must be a U.S. citizen" | eligible |
| `needs_sponsorship` | `false` | "unable to sponsor work visas", "without the need for sponsorship" | not needed |
| `clearance` | `none` / `eligible` / `active` | "Active clearance, or eligibility to obtain one", "clearance eligibility may be required" | US citizens count as eligible |
| `school_country` | `US` | "from a university in Canada (required)", "cannot be enrolled in an accredited U.S. university" | matches |
| `terms` | `[summer-2027, fall-2027]` | title "(Summer 2027)", "beginning May or June 2027" | overlaps |
| `min_hourly_pay` | `40` | "$29.00 per hour", "$1,925 per week", "$10,500/month", "$300,000 per year" | normalized to USD/hour |

Rules: an unset profile field skips its check; a requirement the posting doesn't state never
excludes it; equal-opportunity boilerplate (which mentions citizenship, veterans, disability) is
ignored. Each check appears on the posting as ✓ (met, +2 each up to +10), ⚠ (preferred but not
met, -5) or "filtered:" with the posting's own wording. Requirement chips (term, graduation window,
degree, GPA, authorization, weeks, pay, housing) show on every row and in alerts, even before you
fill in the profile.

## Configuring filters

`config.yaml` → `filters` (run `radar rescore` after changing `filters` or `profile`):

| Key | Default | Meaning |
| --- | --- | --- |
| `role_types` | `[swe, ml, research, applied_science]` | Also: `data`, `hardware`. `[]` = any role |
| `countries` | `[US]` | Where the job is. `[]` = anywhere |
| `allow_remote` | `true` | Fully-remote roles pass but score below on-site/hybrid |
| `allow_unknown_location` | `true` | Keep postings with no parseable location |
| `preferred_cities` | `[]` | +5 when a location matches |
| `preferred_tags` | `[target, ai-lab]` | Registry tags worth +5 |
| `min_score` | `0` | |

**Internship detection** uses, in order: the title (`intern`, `internship`, `co-op`, `student
researcher`, `working student`, and season+year titles such as "FPGA Associate (Summer 2027)"), the
ATS employment type (Ashby `Intern`, Lever commitment `Internship`, Greenhouse metadata), and only
then the description, which counts only with an internship-specific phrase *and* a student or
seasonal title. Word boundaries keep out "Internal Tools", "International", "Database Internals";
titles about interns ("University Recruiter, Interns"), new-grad/entry-level roles, and senior roles
mislabeled "Intern" by their board are excluded. See `tests/test_filters.py`.

**Score**: internship 40, best matching role up to +25, target country +10, on-site/hybrid +5,
preferred city +5, preferred company tag +5, posted in the last 3 days +5, stated requirements you
meet +2 each (max +10), unmet preferences -5 each.

## Configuring alerts

Alerts go by email (Discord and Slack webhooks are also supported). Choose channels in
`config.yaml` (`alerts.channels`) or with `RADAR_ALERT_CHANNELS`. Secrets and the recipient come
from environment variables only:

| Sender | Environment |
| --- | --- |
| SMTP | `SMTP_HOST`, `SMTP_PORT` (587 STARTTLS / 465 SSL), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` |
| Resend | `RESEND_API_KEY`, `EMAIL_FROM` (a verified domain) |
| always | `RADAR_EMAIL_TO` (comma-separated) |
| optional | `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `RADAR_PUBLIC_URL` (linked from digests) |

Each email lists company, title, location, requirement chips, why it matched, and the apply link.
For Gmail, create an [app password](https://support.google.com/accounts/answer/185833) and use
`SMTP_HOST=smtp.gmail.com SMTP_PORT=465 SMTP_USER=you@gmail.com EMAIL_FROM=you@gmail.com`.
Verify with `radar test-alert email`.

## Web app

* `/`: open internships matching your filters, best match first. Filter by company tag, role,
  workplace, term, minimum $/hr, status, location or text; sort by match, newest or pay; "New only" shows postings first seen since your previous
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
  requirements.py  eligibility extraction (grad window, GPA, authorization, ...) + profile checks
  db.py         SQLite schema, snapshot diffing, close tracking
  poller.py     poll cycle, staggering, failure backoff
  alerts.py     formatting, batching, dedupe, senders
  web.py        FastAPI app + templates/
  report.py     coverage report
registry.yaml   companies
config.yaml     filters, alerts, polling
```
