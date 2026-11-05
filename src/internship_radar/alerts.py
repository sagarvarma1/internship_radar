"""Per-user email alerts.

After each poll (every minute):
  1. queue_alerts: every new internship (notify_pending) is scored against every user; matches go
     into user_alerts. Its primary key (user, posting) makes a duplicate alert impossible.
  2. send_instant: users with instant alerts on get unsent matches right away: one email per
     posting, or a single email if more than `batch_threshold` land in the same minute.
  3. send_digests: users with the nightly digest on get one email after their digest hour
     (local time) listing everything that matched since their last digest.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, mailer
from .config import Settings
from .filters import score
from .models import Alert, Remote
from .users import user_config

STALE_AFTER = timedelta(hours=6)  # instant alerts older than this are left to the digest


# ---------------------------------------------------------------- queue


def queue_alerts(conn: sqlite3.Connection, settings: Settings, tags: dict[str, tuple]) -> int:
    rows = conn.execute(
        "SELECT * FROM postings WHERE notify_pending=1 AND closed_at IS NULL AND is_internship=1"
    ).fetchall()
    if not rows:
        return 0
    users = conn.execute("SELECT * FROM users").fetchall()
    queued, t, now = 0, db.now(), datetime.now(UTC)
    parsed = [(r, db.row_to_posting(r), db.row_to_analysis(r)) for r in rows]
    for u in users:
        cfg = user_config(u, settings)
        for r, p, a in parsed:
            m = score(p, a, cfg, tags.get(r["company_key"], ()), now)
            if m.passes:
                queued += conn.execute(
                    """INSERT OR IGNORE INTO user_alerts (user_id, uid, score, reasons, queued_at)
                       VALUES (?,?,?,?,?)""",
                    (u["id"], r["uid"], m.score, json.dumps(m.reasons), t),
                ).rowcount
    conn.executemany("UPDATE postings SET notify_pending=0 WHERE uid=?", [(r["uid"],) for r in rows])
    return queued


# ---------------------------------------------------------------- formatting (pure)


def _loc(a: Alert, limit: int = 3) -> str:
    s = "; ".join(a.locations[:limit]) + (f" +{len(a.locations) - limit}" if len(a.locations) > limit else "")
    s = s or "Location not listed"
    if a.remote is not Remote.UNKNOWN and a.remote.value not in s.lower():
        s += f" ({a.remote.value})"
    return s


def _why(a: Alert) -> str:
    return "; ".join(r for r in a.reasons if not r.startswith("filtered:"))


def subject_for(alerts: list[Alert], digest: bool = False) -> str:
    if digest:
        n = len(alerts)
        return f"Today's internships: {n} new match{'es' if n != 1 else ''}"
    if len(alerts) == 1:
        return f"New internship: {alerts[0].company} - {alerts[0].title}"
    companies = list(dict.fromkeys(a.company for a in alerts))
    return f"{len(alerts)} new internships: " + ", ".join(companies[:4]) + ("…" if len(companies) > 4 else "")


def format_text(alerts: list[Alert], site_url: str, unsubscribe_url: str) -> str:
    lines = []
    for a in alerts:
        lines += [f"{a.company}: {a.title}", f"  Where: {_loc(a)}"]
        if a.chips:
            lines.append(f"  Requirements: {' · '.join(a.chips)}")
        lines += [f"  Why: {_why(a)}", f"  Apply: {a.apply_url}", ""]
    lines += [f"All your matches: {site_url}", f"Email settings: {site_url}/settings",
              f"Unsubscribe from all emails: {unsubscribe_url}"]
    return "\n".join(lines)


def format_html(alerts: list[Alert], site_url: str, unsubscribe_url: str, heading: str = "") -> str:
    e = html.escape
    items = []
    for a in alerts:
        chips = "".join(
            f'<span style="display:inline-block;font-size:12px;padding:1px 7px;margin:4px 4px 0 0;'
            f'border:1px solid #e4e4e7;border-radius:6px;color:#3f3f46">{e(c)}</span>' for c in a.chips)
        items.append(f"""<tr><td style="padding:16px 0;border-bottom:1px solid #f0f0f2">
<div style="font-size:13px;color:#71717a">{e(a.company)}</div>
<div style="font-size:16px;font-weight:600;margin:2px 0 4px"><a href="{e(a.apply_url)}" style="color:#18181b;text-decoration:none">{e(a.title)}</a></div>
<div style="font-size:13px;color:#52525b">{e(_loc(a))}</div><div>{chips}</div>
<div style="margin-top:10px"><a href="{e(a.apply_url)}" style="display:inline-block;background:#4f46e5;color:#fff;font-size:13px;font-weight:600;padding:7px 14px;border-radius:8px;text-decoration:none">Apply</a></div>
</td></tr>""")
    head = f'<h1 style="font-size:18px;margin:0 0 4px">{e(heading)}</h1>' if heading else ""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;margin:0 auto;color:#18181b">'
        f'{head}<table style="width:100%;border-collapse:collapse">{"".join(items)}</table>'
        f'<p style="font-size:12px;color:#71717a;margin-top:20px"><a href="{e(site_url)}" style="color:#71717a">All your matches</a> · '
        f'<a href="{e(site_url)}/settings" style="color:#71717a">Email settings</a> · '
        f'<a href="{e(unsubscribe_url)}" style="color:#71717a">Unsubscribe</a></p></div>'
    )


def build_message(to: str, alerts: list[Alert], site_url: str, unsubscribe_url: str,
                  digest: bool = False) -> mailer.Message:
    heading = subject_for(alerts, digest) if digest else ""
    return mailer.Message(
        to=to, subject=subject_for(alerts, digest),
        text=format_text(alerts, site_url, unsubscribe_url),
        html=format_html(alerts, site_url, unsubscribe_url, heading),
        headers={"List-Unsubscribe": f"<{unsubscribe_url}>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    )


# ---------------------------------------------------------------- send


def _alerts(conn: sqlite3.Connection, user_id: int, where: str) -> list[Alert]:
    rows = conn.execute(
        f"""SELECT a.*, p.company, p.title, p.locations, p.remote, p.apply_url, p.chips
            FROM user_alerts a JOIN postings p ON p.uid=a.uid
            WHERE a.user_id=? AND p.closed_at IS NULL AND {where}
            ORDER BY a.score DESC, a.queued_at""",
        (user_id,),
    ).fetchall()
    return [Alert(uid=r["uid"], company=r["company"], title=r["title"],
                  locations=json.loads(r["locations"]), remote=Remote(r["remote"]),
                  apply_url=r["apply_url"], score=r["score"], reasons=json.loads(r["reasons"]),
                  first_seen=datetime.fromisoformat(r["queued_at"]), chips=json.loads(r["chips"]))
            for r in rows]


def _send(user: sqlite3.Row, batch: list[Alert], settings: Settings, digest: bool) -> bool:
    site = settings.site_url
    unsub = f"{site}/unsubscribe/{user['unsubscribe_token']}"
    try:
        mailer.send(build_message(user["email"], batch, site, unsub, digest))
        return True
    except Exception as e:  # one bad address must not stop everyone else's alerts
        print(f"email to user {user['id']} failed: {type(e).__name__}: {e}", flush=True)
        return False


def send_instant(conn: sqlite3.Connection, settings: Settings, at: datetime | None = None) -> int:
    at = at or datetime.now(UTC)
    cutoff = (at - STALE_AFTER).isoformat(timespec="seconds")
    sent = 0
    for u in conn.execute("SELECT * FROM users WHERE instant_alerts=1").fetchall():
        # too old for an "it just dropped" email (e.g. mail was down): the digest will cover it
        conn.execute("UPDATE user_alerts SET sent_at='skipped' WHERE user_id=? AND sent_at IS NULL "
                     "AND queued_at<?", (u["id"], cutoff))
        alerts = _alerts(conn, u["id"], "a.sent_at IS NULL")
        if not alerts:
            continue
        batches = [[a] for a in alerts] if len(alerts) <= settings.alerts.batch_threshold else [alerts]
        for batch in batches:
            if not _send(u, batch, settings, digest=False):
                break
            t = db.now()
            conn.executemany("UPDATE user_alerts SET sent_at=? WHERE user_id=? AND uid=?",
                             [(t, u["id"], a.uid) for a in batch])
            sent += 1
    # users with instant alerts off: nothing will ever send these, mark them handled
    conn.execute("""UPDATE user_alerts SET sent_at='off' WHERE sent_at IS NULL AND user_id IN
                    (SELECT id FROM users WHERE instant_alerts=0)""")
    return sent


def digest_due(user: sqlite3.Row, at: datetime) -> bool:
    try:
        tz = ZoneInfo(user["timezone"])
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    local = at.astimezone(tz)
    if local.hour < user["digest_hour"]:
        return False
    if not user["last_digest_at"]:
        return True
    return datetime.fromisoformat(user["last_digest_at"]).astimezone(tz).date() < local.date()


def send_digests(conn: sqlite3.Connection, settings: Settings, at: datetime | None = None) -> int:
    at = at or datetime.now(UTC)
    sent = 0
    for u in conn.execute("SELECT * FROM users WHERE nightly_digest=1").fetchall():
        if not digest_due(u, at):
            continue
        alerts = _alerts(conn, u["id"], "a.digested_at IS NULL")
        if alerts:
            if not _send(u, alerts[:50], settings, digest=True):
                continue
            t = db.now()
            conn.executemany("UPDATE user_alerts SET digested_at=? WHERE user_id=? AND uid=?",
                             [(t, u["id"], a.uid) for a in alerts])
            sent += 1
        conn.execute("UPDATE users SET last_digest_at=? WHERE id=?", (db.now(), u["id"]))
    conn.execute("""UPDATE user_alerts SET digested_at='off' WHERE digested_at IS NULL AND user_id IN
                    (SELECT id FROM users WHERE nightly_digest=0)""")
    return sent


def run(conn: sqlite3.Connection, settings: Settings, tags: dict[str, tuple]) -> dict:
    queued = queue_alerts(conn, settings, tags)
    return {"queued": queued, "instant_emails": send_instant(conn, settings),
            "digests": send_digests(conn, settings)}
