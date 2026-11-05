"""Loads config.yaml (filters, alert settings) and registry.yaml (companies).

Secrets never live in these files; they come from environment variables (see README).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .filters import FilterConfig
from .models import ATS, Company

# Project root: the working directory if it holds config/registry files (deployments), else the
# source checkout (editable installs).
ROOT = Path.cwd() if (Path.cwd() / "registry.yaml").exists() else Path(__file__).resolve().parents[2]


@dataclass
class AlertConfig:
    channels: list[str] = field(default_factory=list)  # email, sms, discord, slack, console
    batch_threshold: int = 3  # more than this many alerts at once -> one digest message
    min_score: float = 0.0  # alert only on passing postings at or above this score
    sms_max_items: int = 5  # SMS digests list at most this many titles
    sms_quiet_hours: tuple[int, int] | None = None  # e.g. (23, 7) local time; held, not dropped
    timezone: str = "America/Los_Angeles"
    email_to: list[str] = field(default_factory=list)
    sms_to: list[str] = field(default_factory=list)  # E.164 numbers, e.g. +14155550123
    public_url: str | None = None  # web app URL, linked from digests


@dataclass
class PollConfig:
    interval_minutes: int = 10
    concurrency: int = 6
    per_host_interval_seconds: float = 1.0
    max_backoff_hours: float = 6.0


@dataclass
class Settings:
    filters: FilterConfig
    alerts: AlertConfig
    poll: PollConfig
    db_path: Path
    registry_path: Path


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("RADAR_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    a = dict(raw.get("alerts") or {})
    if a.get("sms_quiet_hours"):
        a["sms_quiet_hours"] = tuple(a["sms_quiet_hours"])
    # Recipients may also come from env so the public repo doesn't hold personal contact info.
    if os.environ.get("RADAR_EMAIL_TO"):
        a["email_to"] = [x.strip() for x in os.environ["RADAR_EMAIL_TO"].split(",")]
    if os.environ.get("RADAR_SMS_TO"):
        a["sms_to"] = [x.strip() for x in os.environ["RADAR_SMS_TO"].split(",")]
    if os.environ.get("RADAR_PUBLIC_URL"):
        a["public_url"] = os.environ["RADAR_PUBLIC_URL"]
    if os.environ.get("RADAR_ALERT_CHANNELS"):
        a["channels"] = [x.strip() for x in os.environ["RADAR_ALERT_CHANNELS"].split(",") if x.strip()]
    return Settings(
        filters=FilterConfig.from_dict(raw.get("filters")),
        alerts=AlertConfig(**a),
        poll=PollConfig(**(raw.get("poll") or {})),
        db_path=Path(os.environ.get("RADAR_DB", raw.get("db_path") or ROOT / "data" / "radar.db")),
        registry_path=Path(
            os.environ.get("RADAR_REGISTRY", raw.get("registry_path") or ROOT / "registry.yaml")
        ),
    )


def load_registry(path: str | Path) -> list[Company]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    companies, seen = [], set()
    for entry in raw.get("companies", []):
        ats = ATS(entry.get("ats", "unsupported"))
        if ats is not ATS.UNSUPPORTED and not entry.get("slug"):
            raise ValueError(f"{entry.get('name')}: ats={ats} requires a slug")
        c = Company(
            name=entry["name"],
            ats=ats,
            slug=entry.get("slug"),
            careers_url=entry.get("careers_url"),
            tags=tuple(entry.get("tags") or ()),
            lever_region=entry.get("lever_region", "global"),
            note=entry.get("note"),
        )
        if c.key in seen:
            raise ValueError(f"duplicate registry entry: {c.key}")
        seen.add(c.key)
        companies.append(c)
    return companies


def save_registry(path: str | Path, companies: list[Company]) -> None:
    def entry(c: Company) -> dict:
        d = {"name": c.name, "ats": str(c.ats)}
        if c.slug:
            d["slug"] = c.slug
        if c.lever_region != "global":
            d["lever_region"] = c.lever_region
        if c.careers_url:
            d["careers_url"] = c.careers_url
        if c.tags:
            d["tags"] = list(c.tags)
        if c.note:
            d["note"] = c.note
        return d

    body = yaml.safe_dump(
        {"companies": [entry(c) for c in sorted(companies, key=lambda c: c.name.lower())]},
        sort_keys=False, allow_unicode=True, width=100,
    )
    Path(path).write_text(
        "# Company registry. Every supported entry was validated against its live ATS API.\n"
        "# Add companies with:  radar discover <careers-url> --add --tags ai-lab\n" + body
    )
