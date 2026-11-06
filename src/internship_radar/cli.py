"""`radar` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import db
from .config import load_companies, load_registry, load_settings, save_registry
from .discover import discover_name, discover_url
from .fetch import Fetcher
from .filters import analyze
from .models import ATS, Company


def cmd_poll(args, settings) -> int:
    from .poller import poll_all

    summary = asyncio.run(
        poll_all(settings, spread_seconds=args.spread, only=args.only, send_alerts=not args.no_alerts)
    )
    print(json.dumps(summary, indent=2))
    # Exit non-zero only if *everything* failed (network down); partial failures show on /health.
    return 1 if summary["companies"] and summary["ok"] == 0 else 0


def cmd_worker(args, settings) -> int:
    from .poller import worker

    asyncio.run(worker(settings))
    return 0


def cmd_discover(args, settings) -> int:
    async def run():
        f = Fetcher()
        try:
            if args.target.startswith("http"):
                cands, others = await discover_url(f, args.target)
            else:
                cands, others = await discover_name(f, args.target), []
        finally:
            await f.aclose()
        return cands, others

    cands, others = asyncio.run(run())
    for c in cands:
        mark = "OK " if c.valid else "BAD"
        extra = f"{c.jobs} jobs" if c.valid else c.error
        name = f" [board says: {c.company_name}]" if c.company_name else ""
        print(f"{mark} {c.ats:<10} {c.slug:<24} {extra}{name}  ({c.evidence})")
    valid = [c for c in cands if c.valid]
    if not valid:
        print(f"No supported ATS board found. Other ATS detected: {', '.join(others) or 'none'}")
    if args.add:
        if not args.name:
            print("--add needs --name", file=sys.stderr)
            return 2
        companies = load_registry(settings.registry_path)
        companies = [c for c in companies if c.name.lower() != args.name.lower()]
        if valid:
            best = max(valid, key=lambda c: c.jobs or 0)
            new = Company(args.name, best.ats, best.slug,
                          args.target if args.target.startswith("http") else None,
                          tuple(args.tags or ()), best.lever_region)
        else:
            new = Company(args.name, ATS.UNSUPPORTED, None,
                          args.target if args.target.startswith("http") else None,
                          tuple(args.tags or ()),
                          note=f"uses {', '.join(others)}" if others else "no supported ATS found")
        companies.append(new)
        save_registry(settings.registry_path, companies)
        print(f"Added {new.name} -> {new.ats} {new.slug or ''}")
    return 0 if valid else 1


def cmd_validate(args, settings) -> int:
    """Fetch every registry board once and report which are broken."""
    from .discover import Candidate, validate

    companies = [c for c in load_companies(settings) if c.ats is not ATS.UNSUPPORTED
                 and c.ats is not ATS.WORKDAY]

    async def run():
        f = Fetcher(retries=1)
        try:
            return await asyncio.gather(*(
                validate(f, Candidate(c.ats, c.slug, c.lever_region)) for c in companies))
        finally:
            await f.aclose()

    bad = 0
    for c, r in zip(companies, asyncio.run(run())):
        if not r.valid:
            bad += 1
        print(f"{'OK ' if r.valid else 'BAD'} {c.name:<28} {c.ats:<10} {c.slug:<22} "
              f"{r.jobs if r.valid else r.error}")
    print(f"\n{len(companies) - bad}/{len(companies)} boards valid")
    return 1 if bad else 0


def cmd_report(args, settings) -> int:
    from .report import coverage_report

    print(coverage_report(settings, markdown=args.markdown))
    return 0


def cmd_reanalyze(args, settings) -> int:
    conn = db.connect(settings.db_path)
    n = db.reanalyze_all(conn, analyze)
    print(f"re-analyzed {n} postings")
    return 0


def cmd_test_email(args, settings) -> int:
    """Send a real email (the newest open internship) to check the mail setup."""
    from datetime import UTC, datetime

    from . import mailer
    from .alerts import build_message
    from .models import Alert, Remote

    conn = db.connect(settings.db_path)
    r = conn.execute("SELECT * FROM postings WHERE closed_at IS NULL AND is_internship=1 "
                     "ORDER BY first_seen_at DESC LIMIT 1").fetchone()
    if not r:
        print("no open internships yet; run `radar poll` first", file=sys.stderr)
        return 1
    a = Alert(r["uid"], r["company"], f"[TEST] {r['title']}", json.loads(r["locations"]), Remote(r["remote"]),
              r["apply_url"], 0, ["test email from `radar test-email`"], datetime.now(UTC), json.loads(r["chips"]))
    mailer.send(build_message(args.to, [a], settings.site_url, f"{settings.site_url}/settings"))
    print(f"sent via {mailer.backend()} to {args.to}")
    return 0


def cmd_discover_all(args, settings) -> int:
    from .bulk_discover import run

    report = asyncio.run(run(settings, args.sources, crawls=args.crawls, max_pages=args.max_pages,
                             limit=args.limit))
    print(json.dumps(report, indent=2))
    print(f"wrote {settings.discovered_path}")
    return 0


def cmd_users(args, settings) -> int:
    conn = db.connect(settings.db_path)
    rows = conn.execute("""SELECT u.id, u.created_at, u.last_login_at, u.instant_alerts, u.nightly_digest,
                             (SELECT COUNT(*) FROM user_alerts a WHERE a.user_id=u.id AND a.sent_at LIKE '20%') sent
                           FROM users u ORDER BY u.id""").fetchall()
    print(f"{len(rows)} users")
    for r in rows:  # no emails printed: keep personal data out of logs
        print(f"  #{r['id']} joined {r['created_at'][:10]}, last login {(r['last_login_at'] or '')[:10]}, "
              f"instant={'on' if r['instant_alerts'] else 'off'}, digest={'on' if r['nightly_digest'] else 'off'}, "
              f"{r['sent']} alerts sent")
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    uvicorn.run("internship_radar.web:app", host=args.host, port=args.port, proxy_headers=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="radar", description="Internship Radar")
    p.add_argument("--config", help="path to config.yaml (default: $RADAR_CONFIG or ./config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("poll", help="poll every board once, then send alerts (cron, every minute)")
    s.add_argument("--spread", type=float, default=0, help="spread requests over N seconds")
    s.add_argument("--only", nargs="*", help="only these companies (name or slug)")
    s.add_argument("--no-alerts", action="store_true")
    s.set_defaults(fn=cmd_poll)

    s = sub.add_parser("worker", help="poll forever on the configured interval")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("discover", help="find a company's ATS board from a careers URL or name")
    s.add_argument("target", help="careers page URL, or a company name to guess slugs for")
    s.add_argument("--add", action="store_true", help="add the result to registry.yaml")
    s.add_argument("--name", help="company name (with --add)")
    s.add_argument("--tags", nargs="*")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("discover-all", help="find companies on each ATS from public sources, validate, "
                                           "write discovered.yaml")
    s.add_argument("--sources", nargs="+", default=["simplify", "commoncrawl"],
                   choices=["simplify", "commoncrawl"])
    s.add_argument("--crawls", type=int, default=2, help="how many recent Common Crawl indexes")
    s.add_argument("--max-pages", type=int, default=30, help="index pages per host per crawl")
    s.add_argument("--limit", type=int, help="validate at most N candidates (testing)")
    s.set_defaults(fn=cmd_discover_all)

    s = sub.add_parser("validate", help="check every board in the registry")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("report", help="coverage report")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("reanalyze", help="re-run internship detection/requirement extraction")
    s.set_defaults(fn=cmd_reanalyze)

    s = sub.add_parser("test-email", help="send a real test alert email")
    s.add_argument("to", help="recipient address")
    s.set_defaults(fn=cmd_test_email)

    s = sub.add_parser("users", help="list users (no emails shown)")
    s.set_defaults(fn=cmd_users)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args, load_settings(args.config))


if __name__ == "__main__":
    sys.exit(main())
