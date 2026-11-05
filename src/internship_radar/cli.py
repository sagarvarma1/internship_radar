"""`radar` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace

from . import db
from .config import load_registry, load_settings, save_registry
from .discover import discover_name, discover_url
from .fetch import Fetcher
from .filters import evaluate
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

    companies = [c for c in load_registry(settings.registry_path) if c.ats is not ATS.UNSUPPORTED]

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


def cmd_rescore(args, settings) -> int:
    conn = db.connect(settings.db_path)
    tags = {c.key: c.tags for c in load_registry(settings.registry_path)}
    n = db.rescore_all(conn, lambda p: evaluate(p, settings.filters, tags.get(p.company_key, ())))
    print(f"rescored {n} postings")
    return 0


def cmd_test_alert(args, settings) -> int:
    """Send one real, currently-open matching posting to a channel, ignoring dedupe."""
    from .alerts import deliver, pending_alerts

    conn = db.connect(settings.db_path)
    row = conn.execute(
        "SELECT uid FROM postings WHERE closed_at IS NULL AND passes=1 ORDER BY score DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("no open matching postings in the DB yet; run `radar poll` first", file=sys.stderr)
        return 1
    conn.execute("UPDATE postings SET alert_pending=1 WHERE uid=?", (row["uid"],))
    alerts = [a for a in pending_alerts(conn, f"test-{args.channel}", 0) if a.uid == row["uid"]]
    conn.execute("UPDATE postings SET alert_pending=0 WHERE uid=?", (row["uid"],))
    cfg = replace(settings.alerts, batch_threshold=1)
    a = alerts[0]
    a.title = f"[TEST] {a.title}"
    deliver(conn, cfg, args.channel, [a])
    conn.execute("DELETE FROM alert_deliveries WHERE uid=? AND channel=?", (a.uid, args.channel))
    print(f"sent test alert for {a.company}: {a.title} via {args.channel}")
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    uvicorn.run("internship_radar.web:app", host=args.host, port=args.port, proxy_headers=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="radar", description="Internship Radar")
    p.add_argument("--config", help="path to config.yaml (default: $RADAR_CONFIG or ./config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("poll", help="poll every board once, then send alerts (for cron)")
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

    s = sub.add_parser("validate", help="check every board in the registry")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("report", help="coverage report")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("rescore", help="re-run filters on stored postings after a config change")
    s.set_defaults(fn=cmd_rescore)

    s = sub.add_parser("test-alert", help="send the top real match to one channel as a test")
    s.add_argument("channel", choices=["email", "discord", "slack", "console"])
    s.set_defaults(fn=cmd_test_alert)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args, load_settings(args.config))


if __name__ == "__main__":
    sys.exit(main())
