"""ATS endpoint definitions and parsers: raw JSON -> list[Posting].

Formats verified against the official docs and live responses (2026-11):
  Ashby:      GET https://api.ashbyhq.com/posting-api/job-board/{board}
              -> {"apiVersion": "1", "jobs": [...]}
  Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
              -> {"jobs": [...], "meta": {"total": n}}; `content` is HTML-entity-encoded HTML
  Lever:      GET https://api.lever.co/v0/postings/{site}?mode=json  (EU: api.eu.lever.co)
              -> [...]; `createdAt` is epoch milliseconds
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

from .models import ATS, Company, Posting, Remote

US_COUNTRY = {"US", "USA", "United States", "United States of America"}


def board_url(company: Company) -> str:
    match company.ats:
        case ATS.ASHBY:
            return f"https://api.ashbyhq.com/posting-api/job-board/{company.slug}"
        case ATS.GREENHOUSE:
            return f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs?content=true"
        case ATS.LEVER:
            host = "api.eu.lever.co" if company.lever_region == "eu" else "api.lever.co"
            return f"https://{host}/v0/postings/{company.slug}?mode=json"
    raise ValueError(f"{company.name}: no fetchable ATS ({company.ats})")


def parse(company: Company, data: Any) -> list[Posting]:
    match company.ats:
        case ATS.ASHBY:
            return parse_ashby(company, data)
        case ATS.GREENHOUSE:
            return parse_greenhouse(company, data)
        case ATS.LEVER:
            return parse_lever(company, data)
    raise ValueError(f"no parser for {company.ats}")


# ---------------------------------------------------------------- helpers

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|h[1-6]|ul|ol|tr)\b[^>]*>", re.I)
_WS = re.compile(r"[ \t\xa0]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = _BLOCK.sub("\n", s)
    s = html.unescape(_TAG.sub("", s))
    s = _WS.sub(" ", s)
    return _NL.sub("\n\n", s).strip()


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _remote_from(value: str | None) -> Remote:
    v = (value or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if v in ("onsite", "inoffice", "inperson"):
        return Remote.ONSITE
    if v == "hybrid":
        return Remote.HYBRID
    if v in ("remote", "fullyremote"):
        return Remote.REMOTE
    return Remote.UNKNOWN


def _remote_from_locations(locations: list[str]) -> Remote:
    """Only infer what the location text actually says; never assume on-site."""
    if not locations:
        return Remote.UNKNOWN
    lowered = [loc.lower() for loc in locations]
    if any("hybrid" in loc for loc in lowered):
        return Remote.HYBRID
    if all("remote" in loc for loc in lowered):
        return Remote.REMOTE
    return Remote.UNKNOWN


def _same_place(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a.startswith(b) or b.startswith(a)


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        i = (i or "").strip()
        if i and i not in seen:
            seen[i] = None
    return list(seen)


# ---------------------------------------------------------------- Ashby


def parse_ashby(company: Company, data: dict) -> list[Posting]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""]
        countries = []
        addr = (j.get("address") or {}).get("postalAddress") or {}
        if addr.get("addressCountry"):
            countries.append(addr["addressCountry"])
        for sec in j.get("secondaryLocations") or []:
            locations.append(sec.get("location") or "")
            c = ((sec.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
            if c:
                countries.append(c)
        remote = _remote_from(j.get("workplaceType"))
        if remote is Remote.UNKNOWN and j.get("isRemote"):
            remote = Remote.REMOTE
        job_url = j.get("jobUrl") or ""
        source_id = j.get("id") or job_url.rstrip("/").rsplit("/", 1)[-1]
        out.append(
            Posting(
                source=ATS.ASHBY,
                source_id=source_id,
                company=company.name,
                company_key=company.key,
                title=(j.get("title") or "").strip(),
                apply_url=job_url or j.get("applyUrl") or "",
                team=j.get("team"),
                department=j.get("department"),
                locations=_dedupe(locations),
                countries=_dedupe(countries),
                remote=remote,
                employment_type=j.get("employmentType"),
                posted_at=_dt(j.get("publishedAt")),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            )
        )
    return out


# ---------------------------------------------------------------- Greenhouse

_GH_SPLIT = re.compile(r"\s*[|;•·]\s*")


def parse_greenhouse(company: Company, data: dict) -> list[Posting]:
    out = []
    for j in data.get("jobs", []):
        loc_name = (j.get("location") or {}).get("name") or ""
        locations = _GH_SPLIT.split(loc_name) if loc_name else []
        for office in j.get("offices") or []:
            loc = office.get("location")
            # Offices often restate the location with more detail ("San Francisco, California"
            # vs "San Francisco, California, United States"); keep only genuinely new places.
            if loc and not any(_same_place(loc, existing) for existing in locations):
                locations.append(loc)
        locations = _dedupe(locations)

        remote = Remote.UNKNOWN
        employment = None
        for m in j.get("metadata") or []:
            name = (m.get("name") or "").lower()
            val = m.get("value")
            if isinstance(val, list):
                val = ", ".join(map(str, val))
            if not isinstance(val, str):
                continue
            if any(k in name for k in ("location type", "workplace", "remote")):
                remote = _remote_from(val) if _remote_from(val) is not Remote.UNKNOWN else remote
            if any(k in name for k in ("employment type", "job type", "employment")):
                employment = val
        if remote is Remote.UNKNOWN:
            remote = _remote_from_locations(locations)

        depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
        out.append(
            Posting(
                source=ATS.GREENHOUSE,
                source_id=str(j["id"]),
                company=company.name,
                company_key=company.key,
                title=(j.get("title") or "").strip(),
                apply_url=j.get("absolute_url") or "",
                department=depts[0] if depts else None,
                team=depts[-1] if len(depts) > 1 else None,
                locations=locations,
                remote=remote,
                employment_type=employment,
                posted_at=_dt(j.get("first_published") or j.get("updated_at")),
                # content is entity-encoded HTML: unescape once to HTML, then strip to text
                description=html_to_text(html.unescape(j.get("content") or "")),
            )
        )
    return out


# ---------------------------------------------------------------- Lever


def parse_lever(company: Company, data: list) -> list[Posting]:
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locations = _dedupe([cats.get("location") or "", *(cats.get("allLocations") or [])])
        remote = _remote_from(j.get("workplaceType"))
        if remote is Remote.UNKNOWN:
            remote = _remote_from_locations(locations)
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text") or "")
            parts.append(html_to_text(lst.get("content")))
        parts.append(j.get("additionalPlain") or "")
        out.append(
            Posting(
                source=ATS.LEVER,
                source_id=j["id"],
                company=company.name,
                company_key=company.key,
                title=(j.get("text") or "").strip(),
                apply_url=j.get("hostedUrl") or j.get("applyUrl") or "",
                team=cats.get("team"),
                department=cats.get("department"),
                locations=locations,
                countries=[j["country"]] if j.get("country") else [],
                remote=remote,
                employment_type=cats.get("commitment"),
                posted_at=_dt(j.get("createdAt")),
                description="\n".join(p for p in parts if p).strip(),
            )
        )
    return out
