"""Workday career sites.

Workday has no official public posting API. Every career site
(https://{tenant}.wd{N}.myworkdayjobs.com/{site}) is rendered from an unauthenticated JSON
endpoint that the page itself calls:

  POST /wday/cxs/{tenant}/{site}/jobs   {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
       -> {"total", "jobPostings": [{title, externalPath, locationsText, postedOn, bulletFields}], "facets"}
  GET  /wday/cxs/{tenant}/{site}{externalPath}
       -> {"jobPostingInfo": {title, jobDescription (HTML), location, additionalLocations, country,
                              startDate, timeType, jobReqId, externalUrl, ...}}

Rules we follow: only career sites the tenant's robots.txt allows (checked at discovery), one shared
rate limit for all of Workday, and as few requests as possible:

  * Most tenants have a "Job Type" facet with an "Intern" value. Each minute we fetch page 1 of
    that filter (newest first). Only if the total or page 1 changed, or every 30 minutes, do we
    page through the (small) intern list to reconcile closures.
  * Job descriptions are fetched once, when a posting first appears.
  * Tenants without an intern facet: page 1 of all jobs (newest first) each minute, keeping titles
    that look like internships; postings disappear only when a periodic "intern" search no longer
    finds them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from . import db
from .ats import html_to_text
from .fetch import Fetcher
from .filters import analyze, detect_internship
from .models import ATS, Company, Posting, Remote

FULL_SYNC_EVERY = timedelta(minutes=30)
FALLBACK_SYNC_EVERY = timedelta(hours=3)
MAX_PAGES = 40  # 800 postings; intern lists are far smaller
INTERN_FACET = re.compile(r"\bintern|co-?op\b|student|apprentice|university|early career|campus", re.I)
WD_URL = re.compile(
    r"https?://(?P<host>[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.I)


def split_slug(slug: str) -> tuple[str, str, str]:
    """'nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite' -> (host, tenant, site)."""
    host, _, site = slug.partition("/")
    return host, host.split(".")[0], site


def parse_url(url: str) -> str | None:
    """Career-site URL -> slug, ignoring locale prefixes and job paths."""
    m = WD_URL.match(url)
    if not m or m.group("site").lower() in ("wday", "job", "jobs"):
        return None
    return f"{m.group('host').lower()}/{m.group('site')}"


def api_base(slug: str) -> str:
    host, tenant, site = split_slug(slug)
    return f"https://{host}/wday/cxs/{tenant}/{site}"


def public_url(slug: str, external_path: str) -> str:
    host, _, site = split_slug(slug)
    return f"https://{host}/{site}{external_path}"


def pick_intern_facet(facets: list[dict]) -> dict | None:
    """Find the facet value meaning "intern" (usually workerSubType = "Intern (Fixed Term)")."""
    best = None
    for f in facets or []:
        param = f.get("facetParameter")
        for v in f.get("values") or []:
            if "count" not in v or not INTERN_FACET.search(v.get("descriptor", "")):
                continue
            score = (2 if re.search(r"\bintern", v["descriptor"], re.I) else 1) + \
                    (1 if param in ("workerSubType", "jobFamilyGroup", "timeType") else 0)
            if best is None or score > best[0]:
                best = (score, {"param": param, "id": v["id"], "label": v["descriptor"], "count": v["count"]})
    return best[1] if best else None


def source_id(job: dict) -> str:
    bullets = job.get("bulletFields") or []
    return str(bullets[0]) if bullets else job.get("externalPath", "")


def to_posting(company: Company, job: dict, detail: dict | None) -> Posting:
    info = (detail or {}).get("jobPostingInfo") or {}
    locs = [info.get("location") or job.get("locationsText") or ""]
    for extra in info.get("additionalLocations") or []:
        locs.append(extra if isinstance(extra, str) else extra.get("descriptor", ""))
    locs = [x for x in dict.fromkeys(l.strip() for l in locs) if x and not re.fullmatch(r"\d+ Locations", x)]
    country = (info.get("country") or {}).get("descriptor")
    remote_raw = str(info.get("remoteType") or "").lower()
    remote = (Remote.REMOTE if "remote" in remote_raw else Remote.HYBRID if "hybrid" in remote_raw
              else Remote.ONSITE if remote_raw in ("on-site", "onsite", "in office") else Remote.UNKNOWN)
    posted = None
    if info.get("startDate"):
        try:
            posted = datetime.fromisoformat(info["startDate"]).replace(tzinfo=UTC)
        except ValueError:
            pass
    sub_type = (detail or {}).get("_intern_label")  # set when the posting came from the intern facet
    return Posting(
        source=ATS.WORKDAY, source_id=source_id(job), company=company.name,
        company_key=company.key, title=(info.get("title") or job.get("title") or "").strip(),
        apply_url=info.get("externalUrl") or public_url(company.slug, job.get("externalPath", "")),
        locations=locs, countries=[country] if country else [], remote=remote,
        employment_type=sub_type or info.get("timeType"), posted_at=posted,
        description=html_to_text(info.get("jobDescription")),
    )


# ---------------------------------------------------------------- polling


async def _page(fetcher: Fetcher, slug: str, offset: int, facet: dict | None, search: str = ""):
    body = {"appliedFacets": {facet["param"]: [facet["id"]]} if facet else {},
            "limit": 20, "offset": offset, "searchText": search}
    return await fetcher.post_json(f"{api_base(slug)}/jobs", body)


async def _all_pages(fetcher, slug, facet, search="") -> tuple[list[dict] | None, str | None]:
    jobs, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        res = await _page(fetcher, slug, offset, facet, search)
        if res.status != 200 or res.data is None:
            return None, res.error or f"HTTP {res.status}"
        batch = res.data.get("jobPostings") or []
        total = res.data.get("total", total) if offset == 0 else total
        jobs += batch
        offset += len(batch)
        if not batch or (total is not None and offset >= total):
            break
    return jobs, None


async def poll(conn: sqlite3.Connection, fetcher: Fetcher, company: Company) -> dict:
    state = db.get_company_state(conn, company.key)
    meta = json.loads(state["meta"] or "{}") if state else {}
    now = datetime.now(UTC)

    if "facet" not in meta:  # first contact: learn which filter means "intern"
        res = await _page(fetcher, company.slug, 0, None)
        if res.status != 200 or res.data is None:
            db.record_failure(conn, company.key, res.error or f"HTTP {res.status}")
            return {"company": company.name, "status": "error", "error": res.error}
        meta["facet"] = pick_intern_facet(res.data.get("facets"))
    facet = meta["facet"]

    res = await _page(fetcher, company.slug, 0, facet)
    if res.status != 200 or res.data is None:
        db.record_failure(conn, company.key, res.error or f"HTTP {res.status}")
        return {"company": company.name, "status": "error", "error": res.error}
    page1, total = res.data.get("jobPostings") or [], res.data.get("total")

    known = {r["source_id"]: r for r in conn.execute(
        "SELECT * FROM postings WHERE company_key=? AND closed_at IS NULL", (company.key,))}
    last_full = datetime.fromisoformat(meta["full_at"]) if meta.get("full_at") else None
    baselined = bool(state and state["baselined"])

    if facet:
        changed = total != meta.get("total") or any(_sid(j) not in known for j in page1)
        due = not baselined or not last_full or now - last_full >= FULL_SYNC_EVERY
        if not changed and not due:
            db.record_not_modified(conn, company.key)
            _save_meta(conn, company.key, meta)
            return {"company": company.name, "status": "not modified"}
        jobs, err = (page1, None) if (total or 0) <= len(page1) else await _all_pages(fetcher, company.slug, facet)
        full = True
    else:
        # No intern facet: newest jobs page, keep intern-looking titles; periodic intern search
        due = not baselined or not last_full or now - last_full >= FALLBACK_SYNC_EVERY
        if due:
            jobs, err = await _all_pages(fetcher, company.slug, None, search="intern")
            full = True
        else:
            jobs, err, full = page1, None, False
        if jobs is not None:
            jobs = [j for j in jobs if _looks_intern(j.get("title", ""))]
    if jobs is None:
        db.record_failure(conn, company.key, err or "listing failed")
        return {"company": company.name, "status": "error", "error": err}

    # Build postings: stored ones reuse their stored description; new ones get one detail fetch.
    snapshot = []
    for j in jobs:
        sid = _sid(j)
        row = known.get(sid)
        if row is not None:
            p = db.row_to_posting(row)
            snapshot.append((p, db.row_to_analysis(row)))
            continue
        det = await fetcher.get_json(f"{api_base(company.slug)}{j.get('externalPath', '')}")
        detail = det.data if det.status == 200 and isinstance(det.data, dict) else None
        if detail is not None and facet:
            detail["_intern_label"] = facet["label"]
        p = to_posting(company, j, detail)
        if facet and not detail:
            p.employment_type = facet["label"]
        snapshot.append((p, analyze(p)))

    counts = db.apply_snapshot(conn, company.key, None, snapshot, full=full)
    meta["total"] = total
    if full:
        meta["full_at"] = now.isoformat(timespec="seconds")
    _save_meta(conn, company.key, meta)
    return {"company": company.name, "status": "ok", **counts}


def _sid(job: dict) -> str:
    return source_id(job)


def _looks_intern(title: str) -> bool:
    p = Posting(ATS.WORKDAY, "", "", "", title, "")
    return detect_internship(p)[0]


def _save_meta(conn: sqlite3.Connection, key: str, meta: dict) -> None:
    conn.execute("UPDATE companies SET meta=? WHERE key=?", (json.dumps(meta), key))
