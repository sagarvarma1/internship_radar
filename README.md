# Internship Radar

A free website that emails you new tech internships the minute they're posted, filtered to the
ones you're actually eligible for.

* **Checked every minute.** ~200 companies' public job boards (Ashby, Greenhouse, Lever).
* **Matched to you.** Each posting's stated requirements (graduation window, degree, major, GPA,
  US citizen / no sponsorship, clearance, term, pay) are extracted and checked against your
  profile, with the posting's own wording shown for every check.
* **Two kinds of email.** Instantly when a match goes live, plus an optional nightly digest.
* **Anyone can sign up.** Email sign-in (link or 6-digit code), no passwords. Each person gets their
  own profile, "For you" list, saved/applied tracking and email preferences.

It never scrapes LinkedIn, Indeed or company career sites. Companies without a public board on
those three ATSs are listed as unsupported on `/companies`.

## How it works

```
cron, every minute ─► radar poll
    192 boards ─► ATS APIs (ETag → mostly 304s, ~3 req/s per host, backoff)
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
* A poll of all 192 boards takes ~35 s (42 s on a cold start), so it fits the one-minute cron;
  a run never overlaps a previous one.
* New users only get emails for postings that appear after they sign up; everything already open
  is on the site.

## Run it locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/sagarvarma1/internship_radar && cd internship_radar
uv sync
uv run radar poll            # first run: baseline of every board (~45 s)
uv run radar serve           # http://localhost:8000
uv run pytest                # 150 tests, offline
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

## Adding companies

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
registry.yaml      companies
config.yaml        site defaults and polling
```
