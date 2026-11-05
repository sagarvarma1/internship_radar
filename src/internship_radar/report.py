"""Coverage report: companies per ATS, unsupported companies, broken boards, open internships."""

from __future__ import annotations

from collections import Counter

from . import db
from .config import Settings, load_registry
from .models import ATS


def coverage_data(settings: Settings) -> dict:
    companies = load_registry(settings.registry_path)
    conn = db.connect(settings.db_path)
    db.sync_registry(conn, companies)
    states = {r["key"]: r for r in conn.execute("SELECT * FROM companies WHERE active=1")}
    per_ats = Counter(str(c.ats) for c in companies if c.ats is not ATS.UNSUPPORTED)
    unsupported = [c for c in companies if c.ats is ATS.UNSUPPORTED]
    broken = [
        states[c.key] for c in companies
        if c.ats is not ATS.UNSUPPORTED and states.get(c.key) and states[c.key]["consecutive_failures"] > 0
    ]
    never = [c for c in companies if c.ats is not ATS.UNSUPPORTED
             and not (states.get(c.key) and states[c.key]["last_success_at"])]
    q = conn.execute(
        """SELECT COUNT(*) AS postings, COALESCE(SUM(is_internship),0) AS internships,
                  COUNT(DISTINCT CASE WHEN is_internship=1
                  THEN company_key END) AS intern_companies
           FROM postings WHERE closed_at IS NULL"""
    ).fetchone()
    top = conn.execute(
        """SELECT company, COUNT(*) AS n FROM postings
           WHERE closed_at IS NULL AND is_internship=1 GROUP BY company ORDER BY n DESC LIMIT 15"""
    ).fetchall()
    last_run = conn.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return dict(
        companies=companies, per_ats=per_ats, unsupported=unsupported, broken=broken, never=never,
        open_postings=q["postings"], open_internships=q["internships"],
        intern_companies=q["intern_companies"], top=top, last_run=last_run, users=users,
    )


def coverage_report(settings: Settings, markdown: bool = False) -> str:
    d = coverage_data(settings)
    supported = sum(d["per_ats"].values())
    lines = [
        "# Internship Radar coverage report" if markdown else "INTERNSHIP RADAR COVERAGE",
        "",
        f"Companies in registry: {len(d['companies'])} ({supported} tracked, {len(d['unsupported'])} unsupported)",
        "Tracked per ATS: " + ", ".join(f"{k} {v}" for k, v in sorted(d["per_ats"].items())),
        f"Open postings tracked: {d['open_postings']}",
        f"Open internships: {d['open_internships']} at {d['intern_companies']} companies",
        f"Users: {d['users']}",
    ]
    if d["last_run"]:
        r = d["last_run"]
        lines.append(f"Last poll: {r['finished_at'] or r['started_at']} ({r['ok']} ok, {r['failed']} failed)")
    lines += ["", "## Most internships" if markdown else "MOST INTERNSHIPS"]
    lines += [f"- {r['company']}: {r['n']}" for r in d["top"]]
    lines += ["", "## Broken boards" if markdown else "BROKEN BOARDS"]
    lines += [f"- {r['name']} ({r['ats']}:{r['slug']}): {r['consecutive_failures']} failures, {r['last_error']}"
              for r in d["broken"]] or ["- none"]
    if d["never"]:
        lines += ["", f"Not yet polled successfully: {', '.join(c.name for c in d['never'])}"]
    lines += ["", "## Unsupported companies" if markdown else "UNSUPPORTED COMPANIES"]
    lines += [f"- {c.name}: {c.note or ''}" for c in d["unsupported"]]
    return "\n".join(lines)
