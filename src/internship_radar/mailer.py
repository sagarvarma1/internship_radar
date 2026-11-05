"""Outgoing email: sign-in links and alerts.

Backend is chosen from env:
  RESEND_API_KEY            -> Resend HTTP API (recommended for a public site)
  SMTP_HOST/USER/PASSWORD   -> SMTP (587 STARTTLS, or 465 SSL via SMTP_PORT)
  neither                   -> console: messages are printed, nothing is sent (development)
EMAIL_FROM is the sender, e.g. "Internship Radar <radar@yourdomain.com>".
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx


class MailError(RuntimeError):
    pass


@dataclass
class Message:
    to: str
    subject: str
    text: str
    html: str
    headers: dict[str, str] = field(default_factory=dict)


def backend() -> str:
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if os.environ.get("SMTP_HOST"):
        return "smtp"
    return "console"


SENT_LOG: list[Message] = []  # console backend keeps messages here (tests, local dev)


def send(msg: Message) -> None:
    kind = backend()
    sender = os.environ.get("EMAIL_FROM", "Internship Radar <radar@localhost>")
    if kind == "console":
        SENT_LOG.append(msg)
        print(f"[mail to {msg.to}] {msg.subject}\n{msg.text}\n", flush=True)
        return
    if kind == "resend":
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
            json={"from": sender, "to": [msg.to], "subject": msg.subject, "text": msg.text,
                  "html": msg.html, "headers": msg.headers},
            timeout=20,
        )
        if r.status_code >= 300:
            raise MailError(f"Resend HTTP {r.status_code}: {r.text[:200]}")
        return
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    em = EmailMessage()
    em["Subject"], em["From"], em["To"] = msg.subject, sender, msg.to
    for k, v in msg.headers.items():
        em[k] = v
    em.set_content(msg.text)
    em.add_alternative(msg.html, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if user:
                    s.login(user, password or "")
                s.send_message(em)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                if user:
                    s.login(user, password or "")
                s.send_message(em)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"SMTP: {e}") from e
