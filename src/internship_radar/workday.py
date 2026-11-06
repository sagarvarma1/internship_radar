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

import asyncio
import hashlib
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
# Facet values meaning "intern". Whole words only: "\bintern" alone matched "Internal Audit" and
# "International"; plain "student"/"university" matched job families like "Student Services".
INTERN_FACET = re.compile(
    r"\b(interns?|internships?|co-?ops?|apprentices?|apprenticeships?|trainees?|work study|"
    r"student( workers?)?|early careers?|campus)\b", re.I)
NOT_INTERN_FACET = re.compile(
    r"\b(services|affairs|audit|internal|international|university of|on[- ]campus|onsite|remote|hybrid)\b",
    re.I)
# facets that describe where/how someone works, never what kind of job it is
NOT_INTERN_PARAMS = {"remoteType", "locations", "locationCountry", "locationRegion", "Location_Country",
                     "locationRegionStateProvince", "timeType"}
# A poll fetches at most this many new descriptions (~1 s each); a bigger backlog is stored in
# chunks over the next runs instead of one poll that never finishes inside the cron budget.
MAX_DETAILS_PER_POLL = 40
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
        if param in NOT_INTERN_PARAMS:
            continue
        for v in f.get("values") or []:
            if "count" not in v or not is_intern_facet(v.get("descriptor", "")):
                continue
            score = (2 if re.search(r"\bintern(s|ships?)?\b", v["descriptor"], re.I) else 1) + \
                    (1 if param in ("workerSubType", "jobFamilyGroup") else 0)
            if best is None or score > best[0]:
                best = (score, {"param": param, "id": v["id"], "label": v["descriptor"], "count": v["count"]})
    return best[1] if best else None


def is_intern_facet(label: str) -> bool:
    return bool(INTERN_FACET.search(label)) and not NOT_INTERN_FACET.search(label)


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


async def _all_pages(fetcher, slug, facet, search="") -> tuple[list[dict] | None, str | None, bool]:
    """Every page of a listing: page 0 gives the total, the rest are fetched concurrently (the
    per-cluster rate limit spaces them). Returns (jobs, error, complete); complete is False when
    the listing is longer than MAX_PAGES, so callers must not treat missing postings as closed."""
    first = await _page(fetcher, slug, 0, facet, search)
    if first.status != 200 or first.data is None:
        return None, first.error or f"HTTP {first.status}", False
    jobs = list(first.data.get("jobPostings") or [])
    total = first.data.get("total") or len(jobs)
    step = len(jobs) or 20
    offsets = list(range(step, min(total, step * MAX_PAGES), step))
    for res in await asyncio.gather(*(_page(fetcher, slug, o, facet, search) for o in offsets)):
        if res.status != 200 or res.data is None:
            return None, res.error or f"HTTP {res.status}", False
        jobs += res.data.get("jobPostings") or []
    return jobs, None, total <= step * MAX_PAGES


async def poll(conn: sqlite3.Connection, fetcher: Fetcher, company: Company) -> dict:
    state = db.get_company_state(conn, company.key)
    meta = json.loads(state["meta"] or "{}") if state else {}
    now = datetime.now(UTC)

    if meta.get("facet") and not is_intern_facet(meta["facet"]["label"]):
        # learned with an older, looser rule (e.g. "Internal Audit"): re-learn, and treat the new
        # facet's listing as a fresh baseline so its existing postings aren't emailed as new
        meta = {}
        conn.execute("UPDATE companies SET baselined=0 WHERE key=?", (company.key,))
        state = db.get_company_state(conn, company.key)
    if "facet" not in meta:  # first contact: learn which filter means "intern"
        res = await _page(fetcher, company.slug, 0, None)
        if res.status != 200 or res.data is None:
            db.record_failure(conn, company.key, res.error or f"HTTP {res.status}")
            return {"company": company.name, "status": "error", "error": res.error}
        meta["facet"] = pick_intern_facet(res.data.get("facets"))
        _save_meta(conn, company.key, meta)  # keep it even if this poll runs out of time
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
        due = not baselined or not last_full or now - last_full >= _spread(FULL_SYNC_EVERY, company.key)
        if not changed and not due:
            db.record_not_modified(conn, company.key)
            _save_meta(conn, company.key, meta)
            return {"company": company.name, "status": "not modified"}
        if (total or 0) <= len(page1):
            jobs, err, full = page1, None, True
        else:
            jobs, err, full = await _all_pages(fetcher, company.slug, facet)
    else:
        # No intern facet: newest jobs page, keep intern-looking titles; periodic intern search
        due = not baselined or not last_full or now - last_full >= _spread(FALLBACK_SYNC_EVERY, company.key)
        if due:
            # "intern" also matches "Internal Medicine" etc.; titles are filtered below
            jobs, err, full = await _all_pages(fetcher, company.slug, None, search="intern")
        else:
            jobs, err, full = page1, None, False
        if jobs is not None:
            jobs = [j for j in jobs if _looks_intern(j.get("title", ""))]
    if jobs is None:
        db.record_failure(conn, company.key, err or "listing failed")
        return {"company": company.name, "status": "error", "error": err}

    # Build postings: stored ones reuse their stored description; new ones get one detail fetch,
    # at most MAX_DETAILS_PER_POLL per poll, fetched concurrently (the per-cluster rate limit in
    # fetch.HOST_GROUPS still spaces them out).
    snapshot, todo = [], []
    for j in jobs:
        row = known.get(_sid(j))
        if row is not None:
            snapshot.append((db.row_to_posting(row), db.row_to_analysis(row)))
        else:
            todo.append(j)
    complete = len(todo) <= MAX_DETAILS_PER_POLL  # otherwise the rest next run
    todo = todo[:MAX_DETAILS_PER_POLL]
    details = await asyncio.gather(*(
        fetcher.get_json(f"{api_base(company.slug)}{j.get('externalPath', '')}") for j in todo))
    for j, det in zip(todo, details):
        detail = det.data if det.status == 200 and isinstance(det.data, dict) else None
        if detail is not None and facet:
            detail["_intern_label"] = facet["label"]
        p = to_posting(company, j, detail)
        if facet and not detail:
            p.employment_type = facet["label"]
        snapshot.append((p, analyze(p)))

    # An incomplete chunk can't close anything (unseen postings may just be unfetched) and
    # leaves total/full_at alone so the next run sees "changed" and continues.
    counts = db.apply_snapshot(conn, company.key, None, snapshot, full=full and complete,
                               complete=complete)
    if complete:
        meta["total"] = total
        if full:
            meta["full_at"] = now.isoformat(timespec="seconds")
    _save_meta(conn, company.key, meta)
    return {"company": company.name, "status": "ok", **counts}


def _spread(every: timedelta, key: str) -> timedelta:
    """every +-20%, fixed per board, so boards baselined together don't all re-sync together."""
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return every * (0.8 + 0.4 * h)


def _sid(job: dict) -> str:
    return source_id(job)


def _looks_intern(title: str) -> bool:
    p = Posting(ATS.WORKDAY, "", "", "", title, "")
    return detect_internship(p)[0]


def _save_meta(conn: sqlite3.Connection, key: str, meta: dict) -> None:
    conn.execute("UPDATE companies SET meta=? WHERE key=?", (json.dumps(meta), key))
