"""SQLite storage. One file, WAL mode, shared by the web app and the poller.

Postings are global (one poll for everyone) and carry their analysis (internship? roles?
requirements?). Everything personal lives in user tables: profile/filters, statuses, alert queue.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ATS, Company, Posting, Remote

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ats TEXT NOT NULL,
    slug TEXT,
    careers_url TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    etag TEXT,
    baselined INTEGER NOT NULL DEFAULT 0,  -- first successful poll done (no alerts on baseline)
    last_polled_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    open_postings INTEGER NOT NULL DEFAULT 0,
    open_internships INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'curated',   -- curated | discovered
    last_internship_at TEXT,                  -- newest first_seen of any internship (polling tier)
    meta TEXT NOT NULL DEFAULT '{}'           -- ATS-specific state (Workday facet, sync times)
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
    -- analysis (independent of who is looking)
    is_internship INTEGER NOT NULL DEFAULT 0,
    intern_reason TEXT NOT NULL DEFAULT '',
    role_types TEXT NOT NULL DEFAULT '[]',
    requirements TEXT NOT NULL DEFAULT '{}',
    chips TEXT NOT NULL DEFAULT '[]',
    notify_pending INTEGER NOT NULL DEFAULT 0  -- new internship: match against users' alerts
);
CREATE INDEX IF NOT EXISTS postings_open ON postings(closed_at, is_internship);
CREATE INDEX IF NOT EXISTS postings_company ON postings(company_key);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    profile TEXT NOT NULL DEFAULT '{}',
    filters TEXT NOT NULL DEFAULT '{}',
    instant_alerts INTEGER NOT NULL DEFAULT 1,    -- email the moment a match is posted
    nightly_digest INTEGER NOT NULL DEFAULT 1,    -- one summary email each evening
    digest_hour INTEGER NOT NULL DEFAULT 20,      -- local hour for the digest
    timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    last_digest_at TEXT,
    visit_prev TEXT,
    visit_cur TEXT,
    unsubscribe_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,              -- 6-digit code, for when the link opens in another browser
    attempts INTEGER NOT NULL DEFAULT 0,
    email TEXT NOT NULL COLLATE NOCASE,
    ip TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS login_tokens_email ON login_tokens(email, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_postings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    status TEXT NOT NULL,                 -- interested | applied | skipped
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, uid)
);

CREATE TABLE IF NOT EXISTS user_alerts (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    score REAL NOT NULL,
    reasons TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    sent_at TEXT,                         -- instant email sent (or 'skipped')
    digested_at TEXT,                     -- included in a nightly digest
    PRIMARY KEY (user_id, uid)            -- a posting is alerted to a user at most once
);
CREATE INDEX IF NOT EXISTS user_alerts_unsent ON user_alerts(sent_at, user_id);

CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    companies INTEGER,
    ok INTEGER,
    failed INTEGER,
    new_postings INTEGER,
    new_internships INTEGER,
    emails_sent INTEGER
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    for name, ddl in (("source", "TEXT NOT NULL DEFAULT 'curated'"), ("last_internship_at", "TEXT"),
                      ("meta", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in have:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {name} {ddl}")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- companies


def sync_registry(conn: sqlite3.Connection, companies: Iterable[Company]) -> None:
    keys = []
    conn.execute("BEGIN")
    for c in companies:
        keys.append(c.key)
        conn.execute(
            """INSERT INTO companies (key, name, ats, slug, careers_url, tags, note, active, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(key) DO UPDATE SET name=excluded.name, careers_url=excluded.careers_url,
                 tags=excluded.tags, note=excluded.note, active=1, source=excluded.source""",
            (c.key, c.name, str(c.ats), c.slug, c.careers_url, json.dumps(list(c.tags)), c.note,
             c.origin),
        )
    placeholders = ",".join("?" * len(keys)) or "''"
    conn.execute(f"UPDATE companies SET active=0 WHERE key NOT IN ({placeholders})", keys)
    # A board no longer watched (slug moved, company dropped from discovery) can't confirm its
    # postings are still open, so close them; they reopen, without alerts, if it comes back.
    conn.execute(
        """UPDATE postings SET closed_at=?, notify_pending=0 WHERE closed_at IS NULL AND company_key IN
             (SELECT key FROM companies WHERE active=0)""", (now(),))
    conn.execute("UPDATE companies SET open_postings=0, open_internships=0 WHERE active=0")
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
        "UPDATE companies SET last_polled_at=?, last_success_at=?, consecutive_failures=0 WHERE key=?",
        (t, t, key),
    )
    conn.execute(
        "UPDATE postings SET last_seen_at=? WHERE company_key=? AND closed_at IS NULL", (t, key)
    )


# ---------------------------------------------------------------- postings


# Descriptions are only needed to extract requirements (internships) or, for a title that
# mentions students/universities, to detect an internship from its description. Everything else
# is stored without one: it's ~90% of postings and ~1 GB at 6,000 boards.
KEEP_DESCRIPTION_TITLE = re.compile(r"\b(students?|university|universit\w+|campus|graduate)\b", re.I)


def stored_description(p: Posting, a) -> str:
    return p.description if a.is_internship or KEEP_DESCRIPTION_TITLE.search(p.title or "") else ""


def apply_snapshot(conn: sqlite3.Connection, company_key: str, etag: str | None,
                   postings: list[tuple[Posting, "object"]], full: bool = True,
                   complete: bool = True) -> dict:
    """Store a board snapshot of (posting, Analysis) pairs. New internships get notify_pending=1
    unless this is the board's baseline (first) snapshot. full=False (a partial listing, e.g.
    Workday's newest page) only adds/updates: nothing missing from it is marked closed.
    complete=False means more of this listing is still to come (a large Workday board stored in
    chunks): if this is the baseline, the board stays un-baselined so the next chunk is too."""
    t = now()
    state = get_company_state(conn, company_key)
    baseline = not (state and state["baselined"])
    seen = set()
    new = new_internships = reopened = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        for p, a in postings:
            if p.uid in seen:
                continue
            seen.add(p.uid)
            row = conn.execute("SELECT closed_at FROM postings WHERE uid=?", (p.uid,)).fetchone()
            values = (
                p.title, p.team, p.department, json.dumps(p.locations), json.dumps(p.countries),
                str(p.remote), p.employment_type,
                p.posted_at.isoformat() if p.posted_at else None, p.apply_url, stored_description(p, a),
                int(a.is_internship), a.intern_reason, json.dumps(a.roles),
                json.dumps(a.requirements.to_json()), json.dumps(a.chips),
            )
            if row is None:
                new += 1
                notify = int(a.is_internship and not baseline)
                new_internships += notify
                conn.execute(
                    """INSERT INTO postings (title, team, department, locations, countries, remote,
                         employment_type, posted_at, apply_url, description, is_internship,
                         intern_reason, role_types, requirements, chips, uid, company_key, source,
                         source_id, company, first_seen_at, last_seen_at, notify_pending)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, p.uid, company_key, str(p.source), p.source_id, p.company, t, t, notify),
                )
            else:
                if row["closed_at"] is not None:
                    reopened += 1
                conn.execute(
                    """UPDATE postings SET title=?, team=?, department=?, locations=?, countries=?,
                         remote=?, employment_type=?, posted_at=?, apply_url=?, description=?,
                         is_internship=?, intern_reason=?, role_types=?, requirements=?, chips=?,
                         last_seen_at=?, closed_at=NULL
                       WHERE uid=?""",
                    (*values, t, p.uid),
                )

        open_rows = conn.execute(
            "SELECT uid FROM postings WHERE company_key=? AND closed_at IS NULL", (company_key,)
        ).fetchall()
        gone = [r["uid"] for r in open_rows if r["uid"] not in seen] if full else []
        conn.executemany(
            "UPDATE postings SET closed_at=?, notify_pending=0 WHERE uid=?", [(t, u) for u in gone]
        )
        counts = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(is_internship), 0) AS i FROM postings
               WHERE company_key=? AND closed_at IS NULL""",
            (company_key,),
        ).fetchone()
        conn.execute(
            """UPDATE companies SET etag=?, baselined=MAX(baselined, ?), last_polled_at=?,
                 last_success_at=?,
                 consecutive_failures=0, open_postings=?, open_internships=?,
                 last_internship_at=COALESCE((SELECT MAX(first_seen_at) FROM postings
                   WHERE company_key=? AND is_internship=1), last_internship_at) WHERE key=?""",
            (etag, int(complete), t, t, counts["n"], counts["i"], company_key, company_key),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"seen": len(seen), "new": new, "new_internships": new_internships,
            "closed": len(gone), "reopened": reopened, "baseline": baseline}


def reanalyze_all(conn: sqlite3.Connection, analyzer) -> int:
    """Re-run posting analysis (after improving detection/extraction). Never queues alerts."""
    rows = conn.execute("SELECT * FROM postings").fetchall()
    conn.execute("BEGIN")
    for r in rows:
        a = analyzer(row_to_posting(r))
        conn.execute(
            """UPDATE postings SET is_internship=?, intern_reason=?, role_types=?, requirements=?,
                 chips=? WHERE uid=?""",
            (int(a.is_internship), a.intern_reason, json.dumps(a.roles),
             json.dumps(a.requirements.to_json()), json.dumps(a.chips), r["uid"]),
        )
    conn.execute(
        """UPDATE companies SET open_internships=(SELECT COUNT(*) FROM postings p
             WHERE p.company_key=companies.key AND p.closed_at IS NULL AND p.is_internship=1)"""
    )
    conn.execute("COMMIT")
    return len(rows)


def row_to_posting(r: sqlite3.Row) -> Posting:
    keys = r.keys()
    return Posting(
        source=ATS(r["source"]), source_id=r["source_id"], company=r["company"],
        company_key=r["company_key"], title=r["title"], apply_url=r["apply_url"],
        team=r["team"], department=r["department"], locations=json.loads(r["locations"]),
        countries=json.loads(r["countries"]), remote=Remote(r["remote"]),
        employment_type=r["employment_type"],
        posted_at=datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None,
        description=r["description"] if "description" in keys else "",
    )


def row_to_analysis(r: sqlite3.Row):
    from .filters import Analysis
    from .requirements import Requirements

    return Analysis(bool(r["is_internship"]), r["intern_reason"], json.loads(r["role_types"]),
                    Requirements.from_json(json.loads(r["requirements"])))


# ---------------------------------------------------------------- users & auth

LOGIN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)


def create_login_token(conn: sqlite3.Connection, email: str, ip: str | None) -> tuple[str, str]:
    """Returns (link token, 6-digit code). Either signs in once; both expire after 15 minutes."""
    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    t = datetime.now(UTC)
    conn.execute(
        """INSERT INTO login_tokens (token_hash, code_hash, email, ip, created_at, expires_at)
           VALUES (?,?,?,?,?,?)""",
        (_hash(token), _hash(f"{email.lower()}:{code}"), email, ip, t.isoformat(timespec="seconds"),
         (t + LOGIN_TTL).isoformat(timespec="seconds")),
    )
    return token, code


def recent_login_requests(conn: sqlite3.Connection, *, email: str | None = None,
                          ip: str | None = None, within: timedelta = timedelta(hours=1)) -> int:
    since = (datetime.now(UTC) - within).isoformat(timespec="seconds")
    if email is not None:
        q, arg = "email=?", email
    elif ip is not None:
        q, arg = "ip=?", ip
    else:
        q, arg = "1=1", None
    args = ([arg] if arg is not None else []) + [since]
    return conn.execute(f"SELECT COUNT(*) FROM login_tokens WHERE {q} AND created_at>?", args).fetchone()[0]


def consume_login_token(conn: sqlite3.Connection, token: str) -> str | None:
    """Single use, unexpired. Returns the email, or None."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM login_tokens WHERE token_hash=?", (_hash(token),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now():
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE login_tokens SET used_at=? WHERE email=? AND used_at IS NULL",
                     (now(), row["email"]))
        conn.execute("COMMIT")
        return row["email"]
    except Exception:
        conn.execute("ROLLBACK")
        raise


MAX_CODE_ATTEMPTS = 5


def consume_login_code(conn: sqlite3.Connection, email: str, code: str) -> bool:
    """Check a 6-digit code against the newest unexpired request for this email.
    Five wrong guesses burn the request."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT * FROM login_tokens WHERE email=? AND used_at IS NULL AND expires_at>?
               ORDER BY created_at DESC LIMIT 1""", (email, now())).fetchone()
        if not row or row["attempts"] >= MAX_CODE_ATTEMPTS:
            conn.execute("COMMIT")
            return False
        ok = secrets.compare_digest(row["code_hash"], _hash(f"{email.lower()}:{code.strip()}"))
        if ok:
            conn.execute("UPDATE login_tokens SET used_at=? WHERE email=? AND used_at IS NULL", (now(), email))
        else:
            conn.execute("UPDATE login_tokens SET attempts=attempts+1 WHERE token_hash=?", (row["token_hash"],))
        conn.execute("COMMIT")
        return ok
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_or_create_user(conn: sqlite3.Connection, email: str, default_filters: dict) -> tuple[sqlite3.Row, bool]:
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), row["id"]))
        return row, False
    conn.execute(
        """INSERT INTO users (email, created_at, last_login_at, filters, unsubscribe_token)
           VALUES (?,?,?,?,?)""",
        (email, now(), now(), json.dumps(default_filters), secrets.token_urlsafe(24)),
    )
    return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone(), True


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    t = datetime.now(UTC)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_hash(token), user_id, t.isoformat(timespec="seconds"),
         (t + SESSION_TTL).isoformat(timespec="seconds")),
    )
    return token


def user_for_session(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token_hash=? AND s.expires_at>?""",
        (_hash(token), now()),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    email = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))  # cascades sessions/statuses/alerts
    if email:
        conn.execute("DELETE FROM login_tokens WHERE email=?", (email["email"],))


CLOSED_JOB_TTL = timedelta(days=7)          # closed non-internships: nothing refers to them
CLOSED_INTERNSHIP_TTL = timedelta(days=180)  # unless someone saved/applied to it


def cleanup(conn: sqlite3.Connection) -> None:
    t = now()
    ago = lambda d: (datetime.now(UTC) - d).isoformat(timespec="seconds")
    conn.execute("DELETE FROM postings WHERE closed_at<? AND is_internship=0", (ago(CLOSED_JOB_TTL),))
    conn.execute(
        """DELETE FROM postings WHERE closed_at<? AND is_internship=1
             AND uid NOT IN (SELECT uid FROM user_postings) AND uid NOT IN (SELECT uid FROM user_alerts)""",
        (ago(CLOSED_INTERNSHIP_TTL),))
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (t,))
    conn.execute("DELETE FROM login_tokens WHERE expires_at<?",
                 ((datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"),))


def set_status(conn: sqlite3.Connection, user_id: int, uid: str, status: str) -> None:
    if status == "new":
        conn.execute("DELETE FROM user_postings WHERE user_id=? AND uid=?", (user_id, uid))
        return
    if status not in ("interested", "applied", "skipped"):
        raise ValueError(status)
    conn.execute(
        """INSERT INTO user_postings (user_id, uid, status, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(user_id, uid) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
        (user_id, uid, status, now()),
    )
