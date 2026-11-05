"""Alert delivery. Dedupe is per (posting uid, channel) in alert_deliveries, so a posting is sent
to each channel at most once, and a failed channel retries next run without re-sending others.

Channels and their env vars:
  email    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, EMAIL_FROM
           or RESEND_API_KEY + EMAIL_FROM
  sms      TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM
  discord  DISCORD_WEBHOOK_URL
  slack    SLACK_WEBHOOK_URL
  console  (prints; for testing)
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import sqlite3
from datetime import UTC, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx

from .config import AlertConfig
from .db import now
from .models import Alert, Remote


class ChannelError(RuntimeError):
    pass


# ---------------------------------------------------------------- collect


def pending_alerts(conn: sqlite3.Connection, channel: str, min_score: float) -> list[Alert]:
    rows = conn.execute(
        """SELECT p.* FROM postings p
           WHERE p.alert_pending=1 AND p.closed_at IS NULL AND p.passes=1 AND p.score>=?
             AND NOT EXISTS (SELECT 1 FROM alert_deliveries d WHERE d.uid=p.uid AND d.channel=?)
           ORDER BY p.score DESC, p.first_seen_at""",
        (min_score, channel),
    ).fetchall()
    return [
        Alert(
            uid=r["uid"], company=r["company"], title=r["title"],
            locations=json.loads(r["locations"]), remote=Remote(r["remote"]),
            apply_url=r["apply_url"], score=r["score"], reasons=json.loads(r["reasons"]),
            first_seen=datetime.fromisoformat(r["first_seen_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------- formatting (pure)


def _loc(a: Alert, limit: int = 3) -> str:
    locs = a.locations[:limit]
    more = f" +{len(a.locations) - limit}" if len(a.locations) > limit else ""
    s = "; ".join(locs) + more or "Location not listed"
    if a.remote is not Remote.UNKNOWN and a.remote.value not in s.lower():
        s += f" ({a.remote.value})"
    return s


def _why(a: Alert) -> str:
    return "; ".join(r for r in a.reasons if not r.startswith("filtered:"))


def format_text(alerts: list[Alert], public_url: str | None = None) -> tuple[str, str]:
    """Plain text (subject, body) used by email, Discord, Slack and console."""
    if len(alerts) == 1:
        a = alerts[0]
        subject = f"New internship: {a.company} - {a.title}"
    else:
        subject = f"{len(alerts)} new internships: " + ", ".join(
            dict.fromkeys(a.company for a in alerts[:4])
        ) + ("…" if len({a.company for a in alerts}) > 4 else "")
    lines = []
    for a in alerts:
        lines += [
            f"{a.company}: {a.title}",
            f"  Where: {_loc(a)}",
            f"  Score {a.score:g}. Why: {_why(a)}",
            f"  Apply: {a.apply_url}",
            "",
        ]
    if public_url:
        lines.append(f"All open internships: {public_url}")
    return subject, "\n".join(lines).strip()


def format_html(alerts: list[Alert], public_url: str | None = None) -> str:
    items = []
    for a in alerts:
        items.append(
            f"""<tr><td style="padding:12px 0;border-bottom:1px solid #e5e5e5">
<div style="font-size:13px;color:#666">{html.escape(a.company)} · score {a.score:g}</div>
<div style="font-size:16px;font-weight:600;margin:2px 0">
<a href="{html.escape(a.apply_url)}" style="color:#1a56db;text-decoration:none">{html.escape(a.title)}</a></div>
<div style="font-size:13px;color:#333">{html.escape(_loc(a))}</div>
<div style="font-size:12px;color:#666;margin-top:4px">Why: {html.escape(_why(a))}</div>
<div style="margin-top:8px"><a href="{html.escape(a.apply_url)}" style="font-size:13px">Apply →</a></div>
</td></tr>"""
        )
    footer = (
        f'<p style="font-size:12px;color:#666"><a href="{html.escape(public_url)}">All open internships</a></p>'
        if public_url else ""
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px">'
        f'<table style="width:100%;border-collapse:collapse">{"".join(items)}</table>{footer}</div>'
    )


def format_sms(alerts: list[Alert], max_items: int, public_url: str | None = None) -> str:
    if len(alerts) == 1:
        a = alerts[0]
        loc = a.locations[0] if a.locations else ""
        return f"New internship: {a.company} - {a.title}" + (f" ({loc})" if loc else "") + f"\n{a.apply_url}"
    lines = [f"{len(alerts)} new internships:"]
    for a in alerts[:max_items]:
        lines.append(f"- {a.company}: {a.title}")
    if len(alerts) > max_items:
        lines.append(f"+{len(alerts) - max_items} more")
    if public_url:
        lines.append(public_url)
    else:
        lines.append(f"Top: {alerts[0].apply_url}")
    return "\n".join(lines)


def batches(alerts: list[Alert], threshold: int) -> list[list[Alert]]:
    """At or below threshold: one message per alert. Above: a single digest."""
    if len(alerts) <= threshold:
        return [[a] for a in alerts]
    return [alerts]


# ---------------------------------------------------------------- senders


def _env(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ChannelError(f"missing env vars: {', '.join(missing)}")
    return [os.environ[n] for n in names]


def send_email(cfg: AlertConfig, subject: str, text: str, html_body: str) -> None:
    if not cfg.email_to:
        raise ChannelError("no email recipients (alerts.email_to or RADAR_EMAIL_TO)")
    (sender,) = _env("EMAIL_FROM")
    if os.environ.get("RESEND_API_KEY"):
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
            json={"from": sender, "to": cfg.email_to, "subject": subject, "text": text, "html": html_body},
            timeout=20,
        )
        if r.status_code >= 300:
            raise ChannelError(f"Resend HTTP {r.status_code}: {r.text[:200]}")
        return
    host, user, password = _env("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(cfg.email_to)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


def send_sms(cfg: AlertConfig, body: str) -> None:
    if not cfg.sms_to:
        raise ChannelError("no SMS recipients (alerts.sms_to or RADAR_SMS_TO)")
    sid, token, sender = _env("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")
    for to in cfg.sms_to:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token), data={"From": sender, "To": to, "Body": body}, timeout=20,
        )
        if r.status_code >= 300:
            raise ChannelError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")


def send_webhook(url_env: str, payload: dict) -> None:
    (url,) = _env(url_env)
    r = httpx.post(url, json=payload, timeout=20)
    if r.status_code >= 300:
        raise ChannelError(f"webhook HTTP {r.status_code}: {r.text[:200]}")


def in_quiet_hours(cfg: AlertConfig, at: datetime | None = None) -> bool:
    if not cfg.sms_quiet_hours:
        return False
    start, end = cfg.sms_quiet_hours
    hour = (at or datetime.now(UTC)).astimezone(ZoneInfo(cfg.timezone)).hour
    return start <= hour or hour < end if start > end else start <= hour < end


def deliver(conn: sqlite3.Connection, cfg: AlertConfig, channel: str, alerts: list[Alert]) -> None:
    for batch in batches(alerts, cfg.batch_threshold):
        subject, text = format_text(batch, cfg.public_url)
        match channel:
            case "email":
                send_email(cfg, subject, text, format_html(batch, cfg.public_url))
            case "sms":
                send_sms(cfg, format_sms(batch, cfg.sms_max_items, cfg.public_url))
            case "discord":
                # Discord caps content at 2000 chars.
                send_webhook("DISCORD_WEBHOOK_URL", {"content": f"**{subject}**\n{text}"[:1990]})
            case "slack":
                send_webhook("SLACK_WEBHOOK_URL", {"text": f"*{subject}*\n{text}"})
            case "console":
                print(f"[alert] {subject}\n{text}\n")
            case _:
                raise ChannelError(f"unknown channel {channel}")
        t = now()
        conn.executemany(
            "INSERT OR IGNORE INTO alert_deliveries (uid, channel, sent_at) VALUES (?,?,?)",
            [(a.uid, channel, t) for a in batch],
        )


def run_alerts(conn: sqlite3.Connection, cfg: AlertConfig) -> dict[str, str]:
    """Send everything pending on every configured channel. Returns per-channel status."""
    status: dict[str, str] = {}
    for channel in cfg.channels:
        alerts = pending_alerts(conn, channel, cfg.min_score)
        if not alerts:
            status[channel] = "nothing pending"
            continue
        if channel == "sms" and in_quiet_hours(cfg):
            status[channel] = f"quiet hours; holding {len(alerts)}"
            continue
        try:
            deliver(conn, cfg, channel, alerts)
            status[channel] = f"sent {len(alerts)}"
        except (ChannelError, httpx.HTTPError, smtplib.SMTPException, OSError) as e:
            status[channel] = f"FAILED: {e}"
    # A posting is done once every configured channel has it (or it no longer qualifies).
    if cfg.channels:
        placeholders = ",".join("?" * len(cfg.channels))
        conn.execute(
            f"""UPDATE postings SET alert_pending=0 WHERE alert_pending=1 AND (
                  closed_at IS NOT NULL OR passes=0 OR score<? OR
                  (SELECT COUNT(DISTINCT channel) FROM alert_deliveries d
                     WHERE d.uid=postings.uid AND d.channel IN ({placeholders})) = ?)""",
            (cfg.min_score, *cfg.channels, len(cfg.channels)),
        )
    return status
