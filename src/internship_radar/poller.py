"""Poll every registered board once (cron mode) or forever (worker mode)."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from . import alerts, db, workday
from .ats import board_url, parse
from .config import Settings, load_companies
from .fetch import Fetcher, HostLimiter
from .filters import analyze
from .models import ATS, Company


def tier_interval(state: sqlite3.Row | None, settings: Settings, at: datetime) -> timedelta:
    """How often a board is polled.
      every minute  curated boards, and discovered boards with an internship open now
      5 minutes     discovered boards that had an internship in the last 180 days
      15 minutes    discovered boards with jobs but no recent internships (still watched, just
                    less often, so a company's first internship is caught within 15 minutes)
      60 minutes    discovered boards with no jobs at all
    """
    base = timedelta(minutes=settings.poll.interval_minutes)
    if state is None or state["source"] != "discovered" or not state["baselined"]:
        return base
    if state["open_internships"]:
        return base
    if state["last_internship_at"] and \
            at - datetime.fromisoformat(state["last_internship_at"]) < timedelta(days=180):
        return timedelta(minutes=5)
    if state["open_postings"]:
        return timedelta(minutes=15)
    return timedelta(minutes=60)


def _due(state: sqlite3.Row | None, settings: Settings, at: datetime) -> bool:
    if state is None or not state["last_polled_at"]:
        return True
    # Back off boards that keep failing: after 3 straight failures, wait interval * 2^(failures-2),
    # capped at max_backoff_hours.
    if state["consecutive_failures"] >= 3 and state["last_error_at"]:
        wait = min(
            timedelta(minutes=settings.poll.interval_minutes) * 2 ** (state["consecutive_failures"] - 2),
            timedelta(hours=settings.poll.max_backoff_hours),
        )
        return at - datetime.fromisoformat(state["last_error_at"]) >= wait
    # 10 s of slack so a board polled at 12:00:03 is due again at the 12:01:00 run
    return at - datetime.fromisoformat(state["last_polled_at"]) >= tier_interval(state, settings, at) - timedelta(seconds=10)


def stagger_offset(key: str, window: float) -> float:
    """Deterministic per-company offset within the polling window, so each board is hit at
    roughly the same point in every cycle instead of all at once."""
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    return (h / 0xFFFFFFFF) * window


async def poll_company(
    conn: sqlite3.Connection, fetcher: Fetcher, company: Company, settings: Settings
) -> dict:
    if company.ats is ATS.WORKDAY:
        return await workday.poll(conn, fetcher, company)
    state = db.get_company_state(conn, company.key)
    etag = state["etag"] if state and state["baselined"] else None
    res = await fetcher.get_json(board_url(company), etag=etag)
    if res.status == 304:
        db.record_not_modified(conn, company.key)
        return {"company": company.name, "status": "not modified"}
    if not res.ok or res.data is None:
        db.record_failure(conn, company.key, res.error or f"HTTP {res.status}")
        return {"company": company.name, "status": "error", "error": res.error}
    try:
        postings = parse(company, res.data)
    except Exception as e:
        db.record_failure(conn, company.key, f"parse error: {type(e).__name__}: {e}")
        return {"company": company.name, "status": "error", "error": str(e)}
    analyzed = [(p, analyze(p)) for p in postings]
    counts = db.apply_snapshot(conn, company.key, res.etag, analyzed)
    return {"company": company.name, "status": "ok", **counts}


async def poll_all(settings: Settings, spread_seconds: float = 0, only: list[str] | None = None,
                   fetcher: Fetcher | None = None, send_alerts: bool = True) -> dict:
    conn = db.connect(settings.db_path)
    companies = load_companies(settings)
    db.sync_registry(conn, companies)
    targets = [c for c in companies if c.ats is not ATS.UNSUPPORTED]
    if only:
        wanted = {o.lower() for o in only}
        targets = [c for c in targets if c.name.lower() in wanted or (c.slug or "").lower() in wanted]
    at = datetime.now(UTC)
    states = {c.key: db.get_company_state(conn, c.key) for c in targets}
    targets = [c for c in targets if _due(states[c.key], settings, at)]
    # most overdue first, so if a run hits its deadline the boards it skipped go first next time
    targets.sort(key=lambda c: (states[c.key]["last_polled_at"] or "") if states[c.key] else "")

    # Polling every minute: never overlap with a run that's still going (a crashed run that never
    # finished stops blocking after 5 minutes).
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM poll_runs WHERE finished_at IS NULL AND started_at>?",
                    (recent,)).fetchone():
        conn.close()
        return {"skipped": "previous run still in progress", "companies": 0, "ok": 0}

    run_id = conn.execute(
        "INSERT INTO poll_runs (started_at, companies) VALUES (?, ?)", (db.now(), len(targets))
    ).lastrowid

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(limiter=HostLimiter(settings.poll.per_host_interval_seconds))
    sem = asyncio.Semaphore(settings.poll.concurrency)
    start = time.monotonic()

    deadline = settings.poll.deadline_seconds

    async def one(c: Company) -> dict:
        if deadline and time.monotonic() - start > deadline:
            # out of time for this minute: leave it for the next run (it's now the most overdue)
            return {"company": c.name, "status": "deferred"}
        if spread_seconds:
            delay = stagger_offset(c.key, spread_seconds) - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
        async with sem:
            try:
                return await poll_company(conn, fetcher, c, settings)
            except Exception as e:  # one bad board must never stop the cycle
                db.record_failure(conn, c.key, f"{type(e).__name__}: {e}")
                return {"company": c.name, "status": "error", "error": str(e)}

    try:
        results = await asyncio.gather(*(one(c) for c in targets))
    finally:
        if own_fetcher:
            await fetcher.aclose()

    alert_status = {}
    if send_alerts:
        tags = {c.key: c.tags for c in companies}
        alert_status = alerts.run(conn, settings, tags)
    db.cleanup(conn)

    deferred = sum(r["status"] == "deferred" for r in results)
    results = [r for r in results if r["status"] != "deferred"]
    ok = sum(r["status"] != "error" for r in results)
    summary = {
        "companies": len(results),
        "deferred": deferred,
        "ok": ok,
        "failed": len(results) - ok,
        "new_postings": sum(r.get("new", 0) for r in results),
        "new_internships": sum(r.get("new_internships", 0) for r in results),
        "closed": sum(r.get("closed", 0) for r in results),
        "seconds": round(time.monotonic() - start, 1),
        "alerts": alert_status,
        "errors": [f"{r['company']}: {r.get('error')}" for r in results if r["status"] == "error"],
    }
    conn.execute(
        """UPDATE poll_runs SET finished_at=?, ok=?, failed=?, new_postings=?, new_internships=?,
             emails_sent=? WHERE id=?""",
        (db.now(), ok, summary["failed"], summary["new_postings"], summary["new_internships"],
         alert_status.get("instant_emails", 0) + alert_status.get("digests", 0), run_id),
    )
    conn.close()
    return summary


async def worker(settings: Settings) -> None:
    """Long-running alternative to cron: one cycle per interval, spread over ~75% of it."""
    interval = settings.poll.interval_minutes * 60
    while True:
        started = time.monotonic()
        summary = await poll_all(settings, spread_seconds=interval * 0.75)
        print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} {summary}", flush=True)
        await asyncio.sleep(max(5.0, interval - (time.monotonic() - started)))
