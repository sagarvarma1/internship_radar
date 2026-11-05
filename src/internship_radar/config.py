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
    batch_threshold: int = 3  # more than this many matches in one minute -> one combined email


@dataclass
class PollConfig:
    interval_minutes: float = 1
    concurrency: int = 12
    per_host_interval_seconds: float = 0.3  # ~3 req/s per ATS host; most answers are 304s
    max_backoff_hours: float = 6.0


@dataclass
class Settings:
    filters: FilterConfig  # defaults for new users and anonymous visitors
    alerts: AlertConfig
    poll: PollConfig
    db_path: Path
    registry_path: Path
    site_url: str  # public URL, used in emails (RADAR_SITE_URL)
    secure_cookies: bool  # true when served over https


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("RADAR_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    site = os.environ.get("RADAR_SITE_URL", raw.get("site_url") or "http://localhost:8000").rstrip("/")
    return Settings(
        filters=FilterConfig.from_dict(raw.get("filters")),
        alerts=AlertConfig(**(raw.get("alerts") or {})),
        poll=PollConfig(**(raw.get("poll") or {})),
        db_path=Path(os.environ.get("RADAR_DB", raw.get("db_path") or ROOT / "data" / "radar.db")),
        registry_path=Path(
            os.environ.get("RADAR_REGISTRY", raw.get("registry_path") or ROOT / "registry.yaml")
        ),
        site_url=site,
        secure_cookies=site.startswith("https://"),
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
