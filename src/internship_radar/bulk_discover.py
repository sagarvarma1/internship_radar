"""Find companies on each ATS at scale, validate them live, write discovered.yaml.

No ATS publishes a directory of its customers, so candidates come from public sources that link to
job boards:

  * Simplify's public GitHub internship / new-grad lists (apply links point at company boards)
  * Common Crawl's URL index (https://index.commoncrawl.org): every page it has crawled on
    jobs.ashbyhq.com, boards.greenhouse.io, jobs.lever.co and *.myworkdayjobs.com

Each candidate is then checked against the live board: it must exist and have open jobs. We record
the company name the board itself reports, how many jobs and internships it has, and for Workday
only career sites that the tenant's robots.txt allows. Re-run weekly to pick up new companies.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote

import yaml

from . import workday
from .ats import board_url, parse
from .discover import PATTERNS
from .fetch import Fetcher, HostLimiter
from .filters import detect_internship
from .models import ATS, Company, Posting

SIMPLIFY_LISTS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README-Off-Season.md",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
]
CC_HOSTS = ["jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
            "jobs.eu.lever.co"]
CC_DOMAINS = ["myworkdayjobs.com"]
IGNORE = {"embed", "api", "v1", "jobs", "js", "static", "assets", "www", "favicon.ico", "robots.txt",
          "sitemap.xml", "careers", "job", "apply", "login", "signin", "home", "search"}


@dataclass
class Found:
    ats: ATS
    slug: str
    region: str = "global"
    source: str = ""
    name: str | None = None
    hint: str | None = None  # company name from the source (e.g. Simplify's table), preferred
    jobs: int = 0
    internships: int = 0
    error: str | None = None


# ---------------------------------------------------------------- candidate extraction (pure)


def candidates_from_text(text: str, source: str) -> dict[tuple, Found]:
    """Board links in text. For Simplify's HTML tables, also the company name from the same row
    (rows starting with "↳" belong to the company above)."""
    if "<tr>" in text:
        out: dict[tuple, Found] = {}
        company = None
        for row in text.split("<tr>")[1:]:
            cells = re.findall(r"<td>(.*?)</td>", row, re.S)
            if not cells:
                continue
            first = re.sub(r"<[^>]+>", "", cells[0]).strip()
            if first and first != "↳":
                company = unescape(first)
            for k, f in _links(row, source).items():
                if company and not f.hint:
                    f.hint = company
                out.setdefault(k, f)
        for k, f in _links(text, source).items():  # anything outside table rows
            out.setdefault(k, f)
        return out
    return _links(text, source)


def _links(text: str, source: str) -> dict[tuple, Found]:
    out: dict[tuple, Found] = {}
    for ats, region, pat in PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).split("?")[0].split("#")[0].strip("/.")
            if not slug or slug.lower() in IGNORE or len(slug) > 60:
                continue
            key = (ats, slug.lower())
            out.setdefault(key, Found(ats, slug, region, source))
    for m in re.finditer(r"https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[^\s)\"'<>]+", text, re.I):
        slug = workday.parse_url(m.group(0))
        if slug:
            out.setdefault((ATS.WORKDAY, slug.lower()), Found(ATS.WORKDAY, slug, source=source))
    return out


JUNK_TITLE = re.compile(
    r"(?i)\b(early careers?|university|students?|interns?(hip)?s?|contractors?|agency|preferential|"
    r"external|internal|search|site|portal|opportunit|talent|recruit|requisition|applicants?|"
    r"workday|welcome|home|current openings|job board)\b")


SITE_NOISE = re.compile(
    r"(?i)^(external|ext|careers?|career|site|sites?|jobs?|job|row|us|usa|global|staff|en|"
    r"page|portal|opportunities|search|candidate|public|main|all|corporate|corp|early|university|"
    r"students?|interns?|internships?|graduates?|campus|faculty|recruiting|\d+)$")
GENERIC_TITLE = re.compile(r"(?i)^(explore|join us|search jobs|find jobs|home|welcome|careers?|jobs?|"
                           r"opportunities|join our team|work with us|apply)$")


def workday_site_name(site: str) -> str | None:
    """'WorldVisionInternational' -> 'World Vision International'; 'UCAR_Careers' -> 'UCAR';
    'NVIDIAExternalCareerSite' -> 'NVIDIA'; 'External' -> None."""
    words = []
    for token in re.split(r"[\s_\-]+", site):
        token = re.sub(r"(?i)(careers?site|careers?|jobs|site|external)$", "", token)
        if not token or SITE_NOISE.match(token):
            continue
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", token) or [token]
        words += [w for w in parts if not SITE_NOISE.match(w)]
    if not words or sum(len(w) for w in words) < 3:
        return None
    return " ".join(w if w.isupper() else w.capitalize() for w in words)


def workday_name(title: str | None, slug: str) -> str:
    """Best display name for a Workday tenant without a name hint, in order: a page title that
    names the company ("Johnson Matthey"); a multi-word name from the career-site path that
    matches the tenant ("WorldVisionInternational" -> "World Vision International"); a title equal
    to the tenant ("AIG", "Air Products"); the tenant itself."""
    host, tenant, site = workday.split_slug(slug)
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    t = clean_name(title, host) if title else ""
    if t and not GENERIC_TITLE.match(t) and norm(t) != norm(tenant):
        return t
    from_site = workday_site_name(site)
    if from_site and (" " in from_site or from_site.isupper()) and norm(from_site)[:4] == norm(tenant)[:4]:
        return from_site
    return t or clean_name(None, host)


def clean_name(raw: str | None, fallback: str) -> str:
    """'Five Rings LLC - Careers' -> 'Five Rings LLC'; 'CAREERS AT NVIDIA' -> 'NVIDIA'."""
    s = unescape(unquote((raw or "").strip()))
    s = re.sub(r"(?i)^\s*(careers|jobs|job openings|work|opportunities)\s+(at|with)\s+", "", s)
    s = re.sub(r"(?i)^(careers|jobs)\s*@\s*", "", s)
    s = re.sub(r"(?i)\s*[-|–—:]\s*(careers?|jobs?|job board|current openings|open positions).*$", "", s)
    s = re.sub(r"(?i)\s+(careers?|jobs?)$", "", s).strip(" -|")
    if GENERIC_TITLE.match(s):
        s = ""
    if not s or len(s) > 60 or s.lower() in ("workday", "careers", "jobs") or JUNK_TITLE.search(s):
        s = re.sub(r"[-_.%]+|20(?=[A-Z])", " ", unquote(fallback).split("/")[0].split(".")[0]).strip()
        s = s.upper() if len(s) <= 3 else s.title()  # "hp" -> "HP", "black-forest-labs" -> "Black Forest Labs"
    if s.isupper() and len(s) > 5 and " " in s:
        s = s.title()
    return s


# ---------------------------------------------------------------- sources


async def from_simplify(fetcher: Fetcher) -> dict[tuple, Found]:
    out: dict[tuple, Found] = {}
    for url in SIMPLIFY_LISTS:
        res = await fetcher.get_text(url)
        if res.status == 200 and res.data:
            for k, v in candidates_from_text(res.data, "simplify").items():
                out.setdefault(k, v)
    return out


async def latest_crawls(fetcher: Fetcher, n: int) -> list[str]:
    res = await fetcher.get_json("https://index.commoncrawl.org/collinfo.json")
    return [c["id"] for c in (res.data or [])[:n]]


async def from_common_crawl(fetcher: Fetcher, crawls: int = 2, max_pages: int = 30) -> dict[tuple, Found]:
    """Every URL Common Crawl has seen on the ATS hosts, reduced to board slugs. The index is
    shared and throttles hard (503 "slow down"), so this uses its own slow, patient client."""
    fetcher = Fetcher(limiter=HostLimiter(1.5), retries=5, backoff_base=5)
    try:
        return await _cc(fetcher, crawls, max_pages)
    finally:
        await fetcher.aclose()


async def _cc(fetcher: Fetcher, crawls: int, max_pages: int) -> dict[tuple, Found]:
    out: dict[tuple, Found] = {}
    for crawl in await latest_crawls(fetcher, crawls):
        api = f"https://index.commoncrawl.org/{crawl}-index"
        queries = [f"url={h}/*" for h in CC_HOSTS] + [f"url={d}&matchType=domain" for d in CC_DOMAINS]
        for q in queries:
            for page in range(max_pages):
                res = await fetcher.get_text(f"{api}?{q}&output=json&fl=url&page={page}")
                if res.status != 200 or not res.data:
                    break
                urls = []
                for line in res.data.splitlines():
                    try:
                        urls.append(json.loads(line)["url"])
                    except (ValueError, KeyError):
                        continue
                if not urls:
                    break
                for k, v in candidates_from_text("\n".join(urls), f"commoncrawl:{crawl}").items():
                    out.setdefault(k, v)
    return out


# ---------------------------------------------------------------- validation


def _count_interns(titles: list[str]) -> int:
    return sum(detect_internship(Posting(ATS.ASHBY, "", "", "", t, ""))[0] for t in titles)


async def _html_title(fetcher: Fetcher, url: str) -> str | None:
    res = await fetcher.get_text(url)
    if res.status != 200 or not res.data:
        return None
    m = re.search(r'property="og:title"\s+content="([^"]+)"', res.data) or \
        re.search(r"<title>([^<]+)</title>", res.data, re.I)
    return m.group(1) if m else None


async def validate(fetcher: Fetcher, f: Found) -> Found:
    try:
        if f.ats is ATS.WORKDAY:
            return await _validate_workday(fetcher, f)
        company = Company(f.slug, f.ats, f.slug, lever_region=f.region)
        url = board_url(company)
        if f.ats is ATS.GREENHOUSE:
            url = url.replace("?content=true", "")  # titles are enough here
        res = await fetcher.get_json(url)
        if res.status != 200 or res.data is None:
            f.error = res.error or f"HTTP {res.status}"
            return f
        titles = ([j.get("title", "") for j in res.data.get("jobs", [])] if f.ats is ATS.GREENHOUSE
                  else [p.title for p in parse(company, res.data)])
        f.jobs, f.internships = len(titles), _count_interns(titles)
        if not f.jobs:
            return f
        if f.ats is ATS.GREENHOUSE:
            meta = await fetcher.get_json(f"https://boards-api.greenhouse.io/v1/boards/{f.slug}")
            raw = (meta.data or {}).get("name") if meta.status == 200 else None
        elif f.ats is ATS.ASHBY:
            raw = await _html_title(fetcher, f"https://jobs.ashbyhq.com/{quote(f.slug)}")
        else:
            host = "jobs.eu.lever.co" if f.region == "eu" else "jobs.lever.co"
            raw = await _html_title(fetcher, f"https://{host}/{f.slug}")
        f.name = clean_name(f.hint, f.slug) if f.hint else clean_name(raw, f.slug)
    except Exception as e:  # malformed board: skip it, keep going
        f.error = f"{type(e).__name__}: {e}"
    return f


_robots_cache: dict[str, str | None] = {}


async def robots_allows(fetcher: Fetcher, host: str, site: str) -> bool:
    if host not in _robots_cache:
        res = await fetcher.get_text(f"https://{host}/robots.txt")
        _robots_cache[host] = res.data if res.status == 200 else None
    txt = _robots_cache[host]
    if txt is None:
        return True  # no robots.txt: nothing disallowed
    disallowed = [l.split(":", 1)[1].strip() for l in txt.splitlines() if l.lower().startswith("disallow:")]
    path = f"/{site}/"
    return not any(d and (d == "/" or path.lower().startswith(d.lower().rstrip("*")))
                   for d in disallowed)


async def _validate_workday(fetcher: Fetcher, f: Found) -> Found:
    host, _, site = workday.split_slug(f.slug)
    if not await robots_allows(fetcher, host, site):
        f.error = "robots.txt disallows this career site"
        return f
    res = await workday._page(fetcher, f.slug, 0, None)
    if res.status != 200 or not isinstance(res.data, dict):
        f.error = res.error or f"HTTP {res.status}"
        return f
    f.jobs = res.data.get("total") or 0
    facet = workday.pick_intern_facet(res.data.get("facets"))
    f.internships = facet["count"] if facet else _count_interns(
        [j.get("title", "") for j in res.data.get("jobPostings") or []])
    if f.jobs:
        f.name = clean_name(f.hint, host) if f.hint else \
            workday_name(await _html_title(fetcher, f"https://{host}/{site}"), f.slug)
    return f


# ---------------------------------------------------------------- orchestration


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"(?i)\b(inc|llc|ltd|corp|corporation|co|technologies|labs)\b", "", name.lower()))


def choose(found: list[Found], curated: list[Company]) -> list[Found]:
    """Keep boards with open jobs; one board per company (curated registry wins, then the board
    with more internships, then more jobs); one Workday career site per tenant."""
    curated_keys = {c.key.lower() for c in curated}
    curated_names = {_norm(c.name) for c in curated}
    live = [f for f in found if not f.error and f.jobs > 0 and f.name]
    # one Workday site per tenant: the one with the most internships
    best_site: dict[str, Found] = {}
    others = []
    for f in live:
        if f.ats is ATS.WORKDAY:
            tenant = f.slug.split("/")[0]
            cur = best_site.get(tenant)
            if cur is None or (f.internships, f.jobs) > (cur.internships, cur.jobs):
                best_site[tenant] = f
        else:
            others.append(f)
    by_name: dict[str, Found] = {}
    for f in others + list(best_site.values()):
        key = f"{f.ats}:{f.slug}".lower()
        n = _norm(f.name)
        if key in curated_keys or n in curated_names or not n:
            continue
        cur = by_name.get(n)
        if cur is None or (f.internships, f.jobs) > (cur.internships, cur.jobs):
            by_name[n] = f
    return sorted(by_name.values(), key=lambda f: f.name.lower())


def write_discovered(path: Path, chosen: list[Found]) -> None:
    entries = []
    for f in chosen:
        e = {"name": f.name, "ats": str(f.ats), "slug": f.slug}
        if f.region != "global":
            e["lever_region"] = f.region
        entries.append(e)
    path.write_text(
        "# GENERATED by `radar discover-all`: companies found via public sources and validated live.\n"
        "# Don't edit by hand; add companies you care about to registry.yaml instead (it wins).\n"
        + yaml.safe_dump({"companies": entries}, sort_keys=False, allow_unicode=True, width=120))


async def run(settings, sources: list[str], crawls: int = 2, max_pages: int = 30,
              limit: int | None = None, progress=print) -> dict:
    from .config import load_registry

    fetcher = Fetcher(limiter=HostLimiter(0.25), retries=1)
    started = time.monotonic()
    try:
        cands: dict[tuple, Found] = {}
        if "simplify" in sources:
            got = await from_simplify(fetcher)
            progress(f"simplify: {len(got)} candidate boards")
            cands.update(got)
        if "commoncrawl" in sources:
            got = await from_common_crawl(fetcher, crawls, max_pages)
            progress(f"common crawl: {len(got)} candidate boards")
            for k, v in got.items():
                cands.setdefault(k, v)
        # Re-check everything found before, so a company doesn't drop out just because this week's
        # sources didn't mention it (it's dropped only if its board is gone or empty).
        if settings.discovered_path.exists():
            prev = yaml.safe_load(settings.discovered_path.read_text()) or {}
            for e in prev.get("companies", []):
                k = (ATS(e["ats"]), e["slug"].lower())
                cands.setdefault(k, Found(ATS(e["ats"]), e["slug"], e.get("lever_region", "global"),
                                          "previous", hint=e["name"]))
            progress(f"previous run: {len(prev.get('companies', []))} companies re-checked")
        found = list(cands.values())[:limit] if limit else list(cands.values())
        progress(f"validating {len(found)} boards live…")
        sem = asyncio.Semaphore(40)
        done = 0

        async def one(f):
            nonlocal done
            async with sem:
                r = await validate(fetcher, f)
            done += 1
            if done % 250 == 0:
                progress(f"  {done}/{len(found)} checked")
            return r

        found = await asyncio.gather(*(one(f) for f in found))
    finally:
        await fetcher.aclose()

    curated = load_registry(settings.registry_path)
    chosen = choose(found, curated)
    write_discovered(settings.discovered_path, chosen)
    by_ats: dict[str, dict] = {}
    for f in found:
        d = by_ats.setdefault(str(f.ats), {"candidates": 0, "live": 0, "kept": 0, "with_internships": 0})
        d["candidates"] += 1
        d["live"] += int(not f.error and f.jobs > 0)
    for f in chosen:
        by_ats[str(f.ats)]["kept"] += 1
        by_ats[str(f.ats)]["with_internships"] += int(f.internships > 0)
    report = {"seconds": round(time.monotonic() - started), "by_ats": by_ats,
              "kept": len(chosen), "with_internships": sum(f.internships > 0 for f in chosen),
              "workday_blocked_by_robots": sum(f.error == "robots.txt disallows this career site" for f in found)}
    settings.discovered_path.with_suffix(".report.json").write_text(json.dumps(report, indent=2))
    return report
