"""Slug discovery: careers URL (or company name) -> validated ATS board.

Reads only the single careers page you point it at, looking for ATS embeds/links. It never
crawls a site or scrapes job listings from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ats import board_url, parse
from .fetch import Fetcher
from .models import ATS, Company

PATTERNS: list[tuple[ATS, str, re.Pattern]] = [
    (ATS.ASHBY, "global", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._%-]+)", re.I)),
    (ATS.ASHBY, "global", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._%-]+)", re.I)),
    (ATS.GREENHOUSE, "global", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    (ATS.GREENHOUSE, "global", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    (ATS.GREENHOUSE, "global", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed\b)([A-Za-z0-9_-]+)", re.I)),
    (ATS.LEVER, "eu", re.compile(r"jobs\.eu\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
    (ATS.LEVER, "eu", re.compile(r"api\.eu\.lever\.co/v0/postings/([A-Za-z0-9._-]+)", re.I)),
    (ATS.LEVER, "global", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
    (ATS.LEVER, "global", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9._-]+)", re.I)),
]
# Other ATSs we recognise so we can say *why* a company is unsupported.
OTHER_ATS = {
    "workday": re.compile(r"myworkdayjobs\.com|wd\d+\.myworkday", re.I),
    "smartrecruiters": re.compile(r"smartrecruiters\.com", re.I),
    "workable": re.compile(r"apply\.workable\.com|workable\.com/", re.I),
    "icims": re.compile(r"icims\.com", re.I),
    "eightfold": re.compile(r"eightfold\.ai", re.I),
    "rippling": re.compile(r"ats\.rippling\.com", re.I),
    "gem": re.compile(r"jobs\.gem\.com", re.I),
    "taleo": re.compile(r"taleo\.net", re.I),
    "successfactors": re.compile(r"successfactors|jobs\.sap\.com", re.I),
}
IGNORE_SLUGS = {"embed", "api", "v1", "jobs", "js", "favicon.ico", "static", "assets", "www"}


@dataclass
class Candidate:
    ats: ATS
    slug: str
    lever_region: str = "global"
    jobs: int | None = None
    evidence: str = ""
    error: str | None = None
    company_name: str | None = None  # name the board reports, where available

    @property
    def valid(self) -> bool:
        return self.jobs is not None and self.error is None


def candidates_from_text(text: str) -> list[Candidate]:
    found: dict[tuple, Candidate] = {}
    for ats, region, pat in PATTERNS:
        for m in pat.finditer(text):
            slug = m.group(1).split("?")[0].split("#")[0].strip("/.")
            if not slug or slug.lower() in IGNORE_SLUGS:
                continue
            key = (ats, slug.lower())
            if key not in found:
                found[key] = Candidate(ats, slug, region, evidence=m.group(0))
    return list(found.values())


def other_ats_in(text: str) -> list[str]:
    return [name for name, pat in OTHER_ATS.items() if pat.search(text)]


def slug_guesses(name: str) -> list[str]:
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    variants = ["".join(words), "-".join(words), "_".join(words), words[0] if words else ""]
    variants += [v + suffix for v in variants[:1] for suffix in ("ai", "hq", "inc", "labs", "jobs")]
    return [v for i, v in enumerate(variants) if v and v not in variants[:i]]


async def validate(fetcher: Fetcher, cand: Candidate) -> Candidate:
    company = Company(name=cand.slug, ats=cand.ats, slug=cand.slug, lever_region=cand.lever_region)
    res = await fetcher.get_json(board_url(company))
    if res.status != 200 or res.data is None:
        cand.error = res.error or f"HTTP {res.status}"
        return cand
    try:
        postings = parse(company, res.data)
    except Exception as e:  # unexpected shape means it isn't really this ATS
        cand.error = f"unparseable response: {e}"
        return cand
    if cand.ats is ATS.GREENHOUSE:
        names = {j.get("company_name") for j in res.data.get("jobs", []) if j.get("company_name")}
        cand.company_name = ", ".join(sorted(names)) or None
    cand.jobs = len(postings)
    return cand


async def discover_url(fetcher: Fetcher, url: str) -> tuple[list[Candidate], list[str]]:
    """Returns (validated-or-failed candidates, names of other ATSs detected)."""
    cands = candidates_from_text(url)
    others: list[str] = []
    if not cands:
        page = await fetcher.client.get(url, headers={"Accept": "text/html"})
        text = page.text + " " + str(page.url)
        cands = candidates_from_text(text)
        others = other_ats_in(text)
    return [await validate(fetcher, c) for c in cands], others


async def discover_name(fetcher: Fetcher, name: str) -> list[Candidate]:
    """Try likely slugs on every supported ATS. Used for bulk seeding; results need a sanity check
    because a slug like 'merge' could belong to a different company."""
    out = []
    for slug in slug_guesses(name):
        for ats in (ATS.GREENHOUSE, ATS.ASHBY, ATS.LEVER):
            c = await validate(fetcher, Candidate(ats, slug, evidence=f"guessed from '{name}'"))
            if c.valid:
                out.append(c)
    return out
