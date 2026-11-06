# Internship Radar

A free website that emails you new tech internships the minute they're posted, filtered to the
ones you're actually eligible for.

* **About 6,000 companies watched**, on Ashby, Greenhouse, Lever and Workday, found automatically
  (see [Finding companies](#finding-companies)). The ~1,700 with internships open, plus the curated
  list, are checked every minute; the rest every 15 minutes.
* **Matched to you.** Each posting's stated requirements (graduation window, degree, major, GPA,
  US citizen / no sponsorship, clearance, term, pay) are extracted and checked against your
  profile, with the posting's own wording shown for every check.
* **Two kinds of email.** Instantly when a match goes live, plus an optional nightly digest.
* **Anyone can sign up.** Email sign-in (link or 6-digit code), no passwords. Each person gets their
  own profile, "For you" list, saved/applied tracking and email preferences.

It never scrapes LinkedIn, Indeed or company career sites. Companies on other systems (iCIMS,
SmartRecruiters, custom sites) are listed as unsupported on `/companies`.

## How it works

```
cron, every minute ─► radar poll
    boards that are due ─► ATS APIs (ETag → mostly 304s, per-API rate limits, backoff)
         │
         ▼
    analyze each posting once: internship? roles? stated requirements?   (stored)
         │
         ▼
    SQLite: diff vs. last poll ─ new internship ─► score against every user's profile
                               ─ missing ─► closed          │
                                                            ▼
                                       user_alerts (one row per user × posting: never twice)
                                            ├─ instant email now (several at once → one email)
                                            └─ nightly digest after the user's chosen hour
web app ─► "For you" (scored per request in ms from the stored analysis), All, Saved, Applied
```

* The first poll of a board is a baseline: everything is stored, nothing is emailed.
* A run polls every board that's due (typically 500-1,700 of them) in 25-50 s. It stops starting
  new boards at 45 s and cancels anything still running at 49 s, so it always fits the one-minute
  cron, and it never overlaps a previous run. Anything it didn't reach goes first next run.
* New users only get emails for postings that appear after they sign up; everything already open
  is on the site.

## Run it locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/sagarvarma1/internship_radar && cd internship_radar
uv sync
uv run radar poll            # first run: starts the baseline (run it repeatedly; see below)
uv run radar serve           # http://localhost:8000
uv run pytest                # 206 tests, offline
```

Without email credentials the site uses a **console mailer**: sign-in links/codes and alerts are
printed in the `radar serve` / `radar poll` output, so you can sign up locally.

Keep polling with cron (`* * * * * cd /path && uv run radar poll`) or `uv run radar worker`.

### Commands

| Command | What it does |
| --- | --- |
| `radar poll [--only NAME…] [--no-alerts]` | Poll every board once, then queue and send emails |
| `radar worker` | Poll forever on `poll.interval_minutes` |
| `radar serve [--port 8000]` | Web app |
| `radar discover <careers-url or name> [--add --name N --tags …]` | Find and validate a company's ATS board |
| `radar discover-all [--sources simplify commoncrawl]` | Find companies on every ATS, validate, write `discovered.yaml` |
| `radar validate` | Fetch every registry board and report broken ones |
| `radar report [--markdown]` | Coverage report |
| `radar reanalyze` | Re-run detection/extraction on stored postings (after changing the code) |
| `radar test-email you@example.com` | Send a real alert email to check mail setup |
| `radar users` | Count users and their email settings (never prints addresses) |

## Configuration

Site-wide settings are in `config.yaml` (default filters for visitors/new users, polling).
Everything personal is per user, stored in the database, and edited on `/settings`.

| Env var | Purpose |
| --- | --- |
| `RADAR_SITE_URL` | Public URL, e.g. `https://radar.example.com` (used in emails; https turns on secure cookies) |
| `RADAR_DB` | SQLite path (default `data/radar.db`) |
| `EMAIL_FROM` | Sender, e.g. `Internship Radar <alerts@radar.example.com>` |
| `RESEND_API_KEY` | Send with [Resend](https://resend.com) (recommended: needs a verified domain) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Or send with any SMTP server |

With neither email option set, mail is printed to the console (development only).

### What the profile checks

| Profile question | Example posting text it's checked against |
| --- | --- |
| Expected graduation | "graduating between September 2027 - July 2028", "by June 2028", "December 2027 or later" |
| (derived) year in school | "Currently a junior", "Rising seniors are preferred", "completed the second year" |
| (derived) school left after internship | "Must be returning to school at the end of the internship" |
| Degree | "Enrolled in a graduate program (Master's or PhD)", titles like "MS/PhD" |
| Major(s) | "pursuing a BA/BS in Accounting, Finance, or a related field" (flagged, not hidden, if "related field") |
| GPA | "GPA of 3.0 or above" ("preferred" only lowers the score) |
| Citizenship / sponsorship | ITAR "Must be a U.S. Person", "U.S. Citizenship… Required", "unable to sponsor work visas" |
| Clearance | "Active clearance, or eligibility to obtain one" |
| School country | "from a university in Canada (required)", "cannot be enrolled in a U.S. university" |
| Terms | title "(Summer 2027)", "beginning May or June 2027" |
| Minimum pay | "$29.00 per hour", "$1,925 per week", "$10,500/month", "$300,000 per year" (→ USD/hr) |

Unanswered questions are skipped, and a requirement a posting doesn't state never hides it.
Equal-opportunity boilerplate (which mentions citizenship, veterans, disability) is ignored.

## Finding companies

No ATS publishes a list of its customers, so `radar discover-all` builds one from public sources:

1. **Simplify's public internship and new-grad lists** on GitHub: every apply link points at a
   company's board.
2. **Common Crawl's URL index** ([index.commoncrawl.org](https://index.commoncrawl.org)): every page
   it has crawled on `jobs.ashbyhq.com`, `boards.greenhouse.io`, `job-boards.greenhouse.io`,
   `jobs.lever.co` and `*.myworkdayjobs.com`, reduced to board slugs.

Each candidate is then checked live: the board must exist and have open jobs. The company name
comes from the board itself (Greenhouse's board name, Ashby/Lever page title, Workday's site
title), and one board is kept per company (the one with more internships). The result goes to
`discovered.yaml` (generated, don't edit). `registry.yaml` stays the hand-curated list with tags,
and wins whenever both have the same company. Re-run weekly:

```sh
uv run radar discover-all            # all sources (~20-40 min, rate limited)
uv run radar discover-all --sources simplify --limit 200   # quick partial run
```

**Polling tiers** keep this polite at scale:

| Tier | Interval | Boards (Nov 2026) |
| --- | --- | --- |
| Curated (`registry.yaml`) or an internship open now | every minute | ~1,690 |
| An internship in the last 180 days | every 5 minutes | few |
| Jobs, but no recent internships | every 15 minutes (±15% per board) | ~3,950 |
| No open jobs | hourly (±15%) | ~380 |

Each run is ordered by how late each board is relative to its own interval, so a backlog of
15-minute boards never delays the every-minute ones. Each API has its own pool of concurrent
requests, so slow Workday calls don't hold up Greenhouse/Ashby, plus a rate limit per API host
(Greenhouse ~12/s, Ashby ~10/s, Lever 5/s, each Workday cluster ≤10/s).

A fresh database takes about 75 runs (~75 minutes of the cron) to baseline all ~6,000 boards,
mostly fetching Workday descriptions, at most 40 per board per run. Nothing is emailed for
baseline postings.

### Workday

Workday has no official posting API. Its career sites are rendered from an unauthenticated JSON
endpoint (`/wday/cxs/{tenant}/{site}/jobs`) that the page itself calls, and that's what Radar
reads. To keep that light:

* only career sites the tenant's `robots.txt` allows (sites it disallows are skipped);
* one rate limit per Workday cluster (`wd1`, `wd5`, ...: ≤10 requests/second each, shared by
  every tenant on it);
* each company's "Job Type = Intern" filter (e.g. `workerSubType = Intern (Fixed Term)`) is
  detected automatically. Location/remote filters and look-alikes ("Internal Audit",
  "International", "Student Services") are rejected. Each minute Radar fetches just the first page
  of that filter (newest first). It pages through the intern list only when something changed,
  or every 30 minutes to catch closures. Descriptions are fetched once per posting;
* companies with no such filter (about half) are searched for "intern" every 3 hours, titles
  filtered, and their newest jobs page checked every minute;
* a listing too long to read in full (over 800 jobs) is never used to close postings.

Steady state is one request per company per minute (NVIDIA: 1 request, under a second).

## Adding companies by hand

```sh
uv run radar discover https://www.notion.com/careers --add --name Notion --tags product
uv run radar discover "Black Forest Labs"     # tries likely slugs on all three ATSs
```

`discover` prints each candidate with its live job count (and, for Greenhouse, the company name the
board reports). **Check the match before adding**: short slugs collide (`ashby:warp` is an HR
startup, not Warp the terminal; `greenhouse:lovable` is an Italian clothing brand). Then run
`radar validate`.

## Privacy and abuse protection

* No passwords. Sign-in links/codes are single-use, expire in 15 minutes, and are stored hashed;
  five wrong codes burn the request.
* Sign-in emails are rate limited (5/hour per address, 20/hour per IP) and the response is the same
  either way, so the form can't be used to probe who has an account.
* Session cookies are HttpOnly, SameSite=Lax, Secure on https; cross-site form posts are rejected.
* Profiles (GPA, citizenship) are only used for matching and are never shown to anyone else.
  Deleting your account removes your profile, statuses, alerts and sessions.
* Every email has a one-click unsubscribe (`List-Unsubscribe` + `List-Unsubscribe-Post`).

## Deploying

See [DEPLOY.md](DEPLOY.md). The site needs a web process, a cron job every minute, and a persistent
disk for SQLite shared by both.

## Layout

```
src/internship_radar/
  models.py        Company, Posting, Match, Alert
  ats.py           endpoint URLs + Ashby/Greenhouse/Lever parsers
  workday.py       Workday career sites (intern facet, incremental sync)
  bulk_discover.py find companies on every ATS (Simplify lists + Common Crawl), validate live
  fetch.py         polite HTTP (ETag, per-host limit, backoff)
  discover.py      careers URL / name -> validated board
  filters.py       internship detection, roles, analyze() (per posting) + score() (per user)
  requirements.py  eligibility extraction + profile checks
  db.py            SQLite: postings, users, sessions, statuses, alert queue
  poller.py        poll cycle, staggering, failure backoff, overlap guard
  alerts.py        per-user queue, instant emails, nightly digest
  mailer.py        Resend / SMTP / console
  users.py         per-user config
  web.py           FastAPI app + templates/
registry.yaml      curated companies (with tags); wins over discovered.yaml
discovered.yaml    generated by `radar discover-all`
config.yaml        site defaults and polling
```
