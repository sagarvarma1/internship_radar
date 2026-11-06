"""Shared interfaces between components.

Every component talks through these three types:
  * Company      - one entry in the registry (registry.yaml)
  * Posting      - one normalized job posting, regardless of source ATS
  * Alert        - one alert payload (a posting plus why it matched)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ATS(StrEnum):
    ASHBY = "ashby"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    UNSUPPORTED = "unsupported"


class Remote(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Company:
    name: str
    ats: ATS
    slug: str | None = None  # Ashby board name / Greenhouse board token / Lever site
    careers_url: str | None = None
    tags: tuple[str, ...] = ()
    lever_region: str = "global"  # "global" or "eu"
    note: str | None = None  # e.g. why it is unsupported
    origin: str = "curated"  # curated (registry.yaml) | discovered (discovered.yaml)

    @property
    def key(self) -> str:
        """Stable identifier used in the DB."""
        return f"{self.ats}:{self.slug}" if self.slug else f"unsupported:{self.name.lower()}"


@dataclass
class Posting:
    source: ATS
    source_id: str  # id from the ATS; unique together with source + company slug
    company: str
    company_key: str
    title: str
    apply_url: str
    team: str | None = None
    department: str | None = None
    locations: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # ISO alpha-2 or alpha-3 as given
    remote: Remote = Remote.UNKNOWN
    employment_type: str | None = None  # raw value from the ATS, e.g. "Intern", "Full-time"
    posted_at: datetime | None = None
    description: str = ""  # plain text

    @property
    def uid(self) -> str:
        return f"{self.company_key}:{self.source_id}"


@dataclass
class Match:
    is_internship: bool
    score: float
    reasons: list[str]  # human-readable, e.g. "title contains 'intern'"
    role_types: list[str]  # e.g. ["swe", "ml"]
    passes: bool  # internship AND passes every configured fit filter
    requirements: dict = field(default_factory=dict)  # extracted eligibility requirements
    chips: list[str] = field(default_factory=list)  # short requirement labels for UI/alerts


@dataclass
class Alert:
    uid: str
    company: str
    title: str
    locations: list[str]
    remote: Remote
    apply_url: str
    score: float
    reasons: list[str]
    first_seen: datetime
    chips: list[str] = field(default_factory=list)
