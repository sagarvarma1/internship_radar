"""SQLite storage. One file, WAL mode, safe for the web app and poller to share."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .models import Company, Match, Posting

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ats TEXT NOT NULL,
    slug TEXT,
    careers_url TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,     -- still in registry
    etag TEXT,
    baselined INTEGER NOT NULL DEFAULT 0,  -- first successful poll done (no alerts on baseline)
    last_polled_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    open_postings INTEGER NOT NULL DEFAULT 0,
    open_internships INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS postings (
    uid TEXT PRIMARY KEY,                 -- company_key:source_id
    company_key TEXT NOT NULL REFERENCES companies(key),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    team TEXT,
    department TEXT,
    locations TEXT NOT NULL DEFAULT '[]',
    countries TEXT NOT NULL DEFAULT '[]',
    remote TEXT NOT NULL,
    employment_type TEXT,
    posted_at TEXT,
    apply_url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    is_internship INTEGER NOT NULL DEFAULT 0,
    passes INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    reasons TEXT NOT NULL DEFAULT '[]',
    role_types TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',   -- new | interested | applied | skipped
    alert_pending INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS postings_open ON postings(closed_at, is_internship, passes);
CREATE INDEX IF NOT EXISTS postings_company ON postings(company_key);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    uid TEXT NOT NULL,
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (uid, channel)
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    companies INTEGER,
    ok INTEGER,
    failed INTEGER,
    new_postings INTEGER,
    new_matches INTEGER
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- companies


def sync_registry(conn: sqlite3.Connection, companies: Iterable[Company]) -> None:
    keys = []
    conn.execute("BEGIN")
    for c in companies:
        keys.append(c.key)
        conn.execute(
            """INSERT INTO companies (key, name, ats, slug, careers_url, tags, note, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(key) DO UPDATE SET name=excluded.name, careers_url=excluded.careers_url,
                 tags=excluded.tags, note=excluded.note, active=1""",
            (c.key, c.name, str(c.ats), c.slug, c.careers_url, json.dumps(list(c.tags)), c.note),
        )
    placeholders = ",".join("?" * len(keys)) or "''"
    conn.execute(f"UPDATE companies SET active=0 WHERE key NOT IN ({placeholders})", keys)
    conn.execute("COMMIT")


def get_company_state(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE key=?", (key,)).fetchone()


def record_failure(conn: sqlite3.Connection, key: str, error: str) -> None:
    t = now()
    conn.execute(
        """UPDATE companies SET last_polled_at=?, last_error_at=?, last_error=?,
             consecutive_failures=consecutive_failures+1 WHERE key=?""",
        (t, t, error[:500], key),
    )


def record_not_modified(conn: sqlite3.Connection, key: str) -> None:
    t = now()
    conn.execute(
        """UPDATE companies SET last_polled_at=?, last_success_at=?, consecutive_failures=0
           WHERE key=?""",
        (t, t, key),
    )
    conn.execute(
        "UPDATE postings SET last_seen_at=? WHERE company_key=? AND closed_at IS NULL", (t, key)
    )


# ---------------------------------------------------------------- postings


def apply_snapshot(
    conn: sqlite3.Connection,
    company_key: str,
    etag: str | None,
    postings: list[tuple[Posting, Match]],
) -> dict:
    """Store a full board snapshot. Returns counts. New matching postings get
    alert_pending=1 unless this is the company's baseline (first) snapshot."""
    t = now()
    state = get_company_state(conn, company_key)
    baseline = not (state and state["baselined"])
    seen = set()
    new = new_matches = reopened = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        for p, m in postings:
            if p.uid in seen:
                continue
            seen.add(p.uid)
            row = conn.execute(
                "SELECT closed_at FROM postings WHERE uid=?", (p.uid,)
            ).fetchone()
            values = (
                p.title, p.team, p.department, json.dumps(p.locations), json.dumps(p.countries),
                str(p.remote), p.employment_type,
                p.posted_at.isoformat() if p.posted_at else None, p.apply_url, p.description,
                int(m.is_internship), int(m.passes), m.score, json.dumps(m.reasons),
                json.dumps(m.role_types),
            )
            if row is None:
                new += 1
                pending = int(m.passes and not baseline)
                new_matches += pending
                conn.execute(
                    """INSERT INTO postings (title, team, department, locations, countries, remote,
                         employment_type, posted_at, apply_url, description, is_internship, passes,
                         score, reasons, role_types, uid, company_key, source, source_id, company,
                         first_seen_at, last_seen_at, alert_pending)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, p.uid, company_key, str(p.source), p.source_id, p.company, t, t,
                     pending),
                )
            else:
                if row["closed_at"] is not None:
                    reopened += 1
                conn.execute(
                    """UPDATE postings SET title=?, team=?, department=?, locations=?, countries=?,
                         remote=?, employment_type=?, posted_at=?, apply_url=?, description=?,
                         is_internship=?, passes=?, score=?, reasons=?, role_types=?,
                         last_seen_at=?, closed_at=NULL
                       WHERE uid=?""",
                    (*values, t, p.uid),
                )

        open_rows = conn.execute(
            "SELECT uid FROM postings WHERE company_key=? AND closed_at IS NULL", (company_key,)
        ).fetchall()
        gone = [r["uid"] for r in open_rows if r["uid"] not in seen]
        conn.executemany(
            "UPDATE postings SET closed_at=?, alert_pending=0 WHERE uid=?", [(t, u) for u in gone]
        )
        counts = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(is_internship), 0) AS i FROM postings
               WHERE company_key=? AND closed_at IS NULL""",
            (company_key,),
        ).fetchone()
        conn.execute(
            """UPDATE companies SET etag=?, baselined=1, last_polled_at=?, last_success_at=?,
                 consecutive_failures=0, open_postings=?, open_internships=? WHERE key=?""",
            (etag, t, t, counts["n"], counts["i"], company_key),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "seen": len(seen), "new": new, "new_matches": new_matches,
        "closed": len(gone), "reopened": reopened, "baseline": baseline,
    }


def rescore_all(conn: sqlite3.Connection, scorer) -> int:
    """Re-run filters over stored postings (after a config change). Never queues alerts."""
    rows = conn.execute("SELECT * FROM postings").fetchall()
    conn.execute("BEGIN")
    for r in rows:
        m = scorer(row_to_posting(r))
        conn.execute(
            """UPDATE postings SET is_internship=?, passes=?, score=?, reasons=?, role_types=?
               WHERE uid=?""",
            (int(m.is_internship), int(m.passes), m.score, json.dumps(m.reasons),
             json.dumps(m.role_types), r["uid"]),
        )
    conn.execute(
        """UPDATE companies SET open_internships=(SELECT COUNT(*) FROM postings p
             WHERE p.company_key=companies.key AND p.closed_at IS NULL AND p.is_internship=1)"""
    )
    conn.execute("COMMIT")
    return len(rows)


def row_to_posting(r: sqlite3.Row) -> Posting:
    from .models import ATS, Remote

    return Posting(
        source=ATS(r["source"]), source_id=r["source_id"], company=r["company"],
        company_key=r["company_key"], title=r["title"], apply_url=r["apply_url"],
        team=r["team"], department=r["department"], locations=json.loads(r["locations"]),
        countries=json.loads(r["countries"]), remote=Remote(r["remote"]),
        employment_type=r["employment_type"],
        posted_at=datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None,
        description=r["description"],
    )


def set_status(conn: sqlite3.Connection, uid: str, status: str) -> None:
    if status not in ("new", "interested", "applied", "skipped"):
        raise ValueError(status)
    conn.execute("UPDATE postings SET status=? WHERE uid=?", (status, uid))
