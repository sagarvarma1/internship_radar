"""Workday parsing (saved real responses), polling logic with a fake fetcher, discovery helpers."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from internship_radar import bulk_discover as bd
from internship_radar import db, workday
from internship_radar.config import AlertConfig, PollConfig, Settings
from internship_radar.fetch import FetchResult
from internship_radar.filters import FilterConfig
from internship_radar.models import ATS, Company
from internship_radar.poller import tier_interval

FIX = Path(__file__).parent / "fixtures"
NV = Company("NVIDIA", ATS.WORKDAY, "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")


def load(name):
    return json.loads((FIX / name).read_text())


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize("url,slug", [
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/X_JR1",
     "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
    ("https://salesforce.wd12.myworkdayjobs.com/en-US/Futureforce_Internships/details/abc",
     "salesforce.wd12.myworkdayjobs.com/Futureforce_Internships"),
    ("https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external/jobs", None),
    ("https://example.com/careers", None),
])
def test_parse_url(url, slug):
    assert workday.parse_url(url) == slug


def test_split_and_urls():
    host, tenant, site = workday.split_slug(NV.slug)
    assert (host, tenant, site) == ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite")
    assert workday.api_base(NV.slug) == \
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
    assert workday.public_url(NV.slug, "/job/X_JR1") == \
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/X_JR1"


@pytest.mark.parametrize("tenant,label,count", [
    ("nvidia", "Intern (Fixed Term)", 211),
    ("salesforce", "Intern (Fixed Term)", 12),   # beats "Fixed Term & Temporary"
    ("adobe", "Intern (Fixed Term)", 28),        # beats jobFamilyGroup "University"
])
def test_pick_intern_facet_real(tenant, label, count):
    f = workday.pick_intern_facet(load(f"workday_{tenant}_jobs.json")["facets"])
    assert f["label"] == label and f["count"] == count and f["param"] == "workerSubType"


def test_pick_intern_facet_none():
    assert workday.pick_intern_facet([{"facetParameter": "timeType", "values": [
        {"descriptor": "Full time", "id": "a", "count": 9}]}]) is None


def test_to_posting_from_real_detail():
    job = {"title": "DFT Intern - 2027", "externalPath": "/job/China-Shanghai/DFT-Intern---2027_JR2027176",
           "bulletFields": ["JR2027176"], "locationsText": "China, Shanghai"}
    detail = load("workday_nvidia_detail.json")
    detail["_intern_label"] = "Intern (Fixed Term)"
    p = workday.to_posting(NV, job, detail)
    assert p.source is ATS.WORKDAY and p.source_id == "JR2027176"
    assert p.uid == f"workday:{NV.slug}:JR2027176"
    assert p.title == "DFT Intern - 2027" and p.locations == ["China, Shanghai"]
    assert p.countries == ["China"] and p.employment_type == "Intern (Fixed Term)"
    assert p.apply_url.startswith("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/")
    assert p.posted_at == datetime(2026, 10, 13, tzinfo=UTC)
    assert p.description and "<p>" not in p.description


# ---------------------------------------------------------------- polling with a fake fetcher


class FakeWorkday:
    """Serves the CXS endpoints from an in-memory list of intern jobs."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []

    async def post_json(self, url, body):
        self.calls.append(("POST", body["offset"], bool(body["appliedFacets"])))
        facets = [{"facetParameter": "workerSubType", "values": [
            {"descriptor": "Intern (Fixed Term)", "id": "F1", "count": len(self.jobs)},
            {"descriptor": "Regular", "id": "R1", "count": 500}]}]
        page = self.jobs[body["offset"]: body["offset"] + 20]
        return FetchResult(200, {"total": len(self.jobs), "jobPostings": page, "facets": facets})

    async def get_json(self, url, etag=None):
        self.calls.append(("GET", url.rsplit("/", 1)[-1]))
        jid = url.rsplit("_", 1)[-1]
        return FetchResult(200, {"jobPostingInfo": {
            "title": f"Software Engineer Intern {jid}", "location": "US, CA, Santa Clara",
            "country": {"descriptor": "United States of America"}, "startDate": "2026-11-01",
            "jobDescription": "<p>This is a Summer 2027 internship.</p><p>Must be graduating by June 2028.</p>",
            "externalUrl": f"https://x/{jid}"}})


def job(i):
    return {"title": f"Software Engineer Intern {i}", "externalPath": f"/job/US/SWE-Intern_JR{i}",
            "bulletFields": [f"JR{i}"], "postedOn": "Posted Today"}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "w.db")
    db.sync_registry(c, [NV])
    return c


def run(coro):
    return asyncio.run(coro)


def test_workday_baseline_then_cheap_then_new_and_closed(conn):
    fake = FakeWorkday([job(i) for i in range(25)])
    r = run(workday.poll(conn, fake, NV))
    assert r["status"] == "ok" and r["new"] == 25 and r["baseline"]
    assert sum(c[0] == "GET" for c in fake.calls) == 25  # one detail fetch per posting, once
    row = conn.execute("SELECT * FROM postings LIMIT 1").fetchone()
    assert row["is_internship"] and "Summer 2027" in row["chips"]

    fake.calls.clear()
    assert run(workday.poll(conn, fake, NV))["status"] == "not modified"
    assert fake.calls == [("POST", 0, True)]  # steady state: a single request

    fake.jobs.insert(0, job(99))  # new posting appears at the top (newest first)
    fake.calls.clear()
    r = run(workday.poll(conn, fake, NV))
    assert r["new"] == 1 and r["new_internships"] == 1
    assert [c for c in fake.calls if c[0] == "GET"] == [("GET", "SWE-Intern_JR99")]
    assert conn.execute("SELECT notify_pending FROM postings WHERE source_id='JR99'").fetchone()[0] == 1

    del fake.jobs[5]  # a posting is taken down -> total changes -> full reconcile closes it
    r = run(workday.poll(conn, fake, NV))
    assert r["closed"] == 1


def test_workday_full_sync_every_30_minutes(conn):
    fake = FakeWorkday([job(i) for i in range(3)])
    run(workday.poll(conn, fake, NV))
    meta = json.loads(conn.execute("SELECT meta FROM companies").fetchone()[0])
    meta["full_at"] = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    conn.execute("UPDATE companies SET meta=?", (json.dumps(meta),))
    assert run(workday.poll(conn, fake, NV))["status"] == "ok"  # reconciled, not "not modified"


def test_workday_error_recorded(conn):
    class Down(FakeWorkday):
        async def post_json(self, url, body):
            return FetchResult(503, error="HTTP 503")
    r = run(workday.poll(conn, Down([]), NV))
    assert r["status"] == "error"
    assert db.get_company_state(conn, NV.key)["consecutive_failures"] == 1


# ---------------------------------------------------------------- polling tiers


def settings(tmp_path):
    return Settings(FilterConfig(), AlertConfig(), PollConfig(), tmp_path / "x.db", Path("registry.yaml"),
                    tmp_path / "d.yaml", "http://x", False)


def test_tiers(tmp_path):
    s = settings(tmp_path)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    base = {"source": "discovered", "baselined": 1, "open_internships": 0, "last_internship_at": None,
            "open_postings": 10}
    assert tier_interval({**base, "source": "curated"}, s, now) == timedelta(minutes=1)
    assert tier_interval({**base, "open_internships": 3}, s, now) == timedelta(minutes=1)
    recent = (now - timedelta(days=40)).isoformat()
    assert tier_interval({**base, "last_internship_at": recent}, s, now) == timedelta(minutes=5)
    assert tier_interval(base, s, now) == timedelta(minutes=15)
    assert tier_interval({**base, "open_postings": 0}, s, now) == timedelta(minutes=60)
    assert tier_interval({**base, "baselined": 0}, s, now) == timedelta(minutes=1)


# ---------------------------------------------------------------- discovery helpers


def test_candidates_from_text():
    text = """
    | **Ramp** | SWE Intern | NYC | <a href="https://jobs.ashbyhq.com/ramp/abc/application?utm_source=Simplify">Apply</a> |
    | Figma | Intern | SF | https://job-boards.greenhouse.io/figma/jobs/123?gh_jid=123 |
    | Palantir | Intern | NYC | https://jobs.lever.co/palantir/xyz/apply |
    | NVIDIA | Intern | CA | https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/X_JR1 |
    https://boards.greenhouse.io/embed/job_board?for=stripe  https://jobs.ashbyhq.com/api/x
    """
    got = bd.candidates_from_text(text, "test")
    keys = {(str(a), s) for a, s in got}
    assert ("ashby", "ramp") in keys and ("greenhouse", "figma") in keys
    assert ("lever", "palantir") in keys and ("greenhouse", "stripe") in keys
    assert ("workday", "nvidia.wd5.myworkdayjobs.com/nvidiaexternalcareersite") in keys
    assert ("ashby", "api") not in keys and ("greenhouse", "embed") not in keys


@pytest.mark.parametrize("raw,fallback,name", [
    ("Five Rings LLC - Careers", "fiveringsllc", "Five Rings LLC"),
    ("CAREERS AT NVIDIA", "nvidia.wd5.myworkdayjobs.com", "NVIDIA"),
    ("Ramp Jobs", "ramp", "Ramp"),
    ("Palantir Technologies", "palantir", "Palantir Technologies"),
    ("Workday", "acme-corp.wd1.myworkdayjobs.com", "Acme Corp"),
    (None, "black-forest-labs", "Black Forest Labs"),
    ("Early Careers at Workday", "workday.wd5.myworkdayjobs.com", "Workday"),
    ("EU Agency Contractor and US Preferential", "hp.wd5.myworkdayjobs.com", "HP"),
    (None, "Ricursive%20Intelligence", "Ricursive Intelligence"),
])
def test_clean_name(raw, fallback, name):
    assert bd.clean_name(raw, fallback) == name


def test_simplify_rows_give_names():
    html = """<tr>
<td><strong><a href="https://simplify.jobs/c/Zebra">Zebra Technologies</a></strong></td><td>SWE Intern</td>
<td><a href="https://zebra.wd501.myworkdayjobs.com/Zebra_careers/job/X_JR1?utm_source=Simplify">Apply</a></td></tr>
<tr><td>↳</td><td>ML Intern</td><td><a href="https://jobs.ashbyhq.com/zebra-ai/123">Apply</a></td></tr>"""
    got = bd.candidates_from_text(html, "simplify")
    hints = {f.slug: f.hint for f in got.values()}
    assert hints == {"zebra.wd501.myworkdayjobs.com/Zebra_careers": "Zebra Technologies",
                     "zebra-ai": "Zebra Technologies"}


def test_choose_dedupes_and_respects_curated():
    curated = [Company("Ramp", ATS.ASHBY, "ramp")]
    F = bd.Found
    found = [
        F(ATS.GREENHOUSE, "ramp", name="Ramp", jobs=1),                  # curated name wins
        F(ATS.GREENHOUSE, "kalshi", name="Kalshi", jobs=28, internships=0),
        F(ATS.ASHBY, "kalshi", name="Kalshi", jobs=35, internships=2),   # same company: more interns
        F(ATS.LEVER, "dead", name="Dead Co", jobs=0),                    # no jobs: dropped
        F(ATS.ASHBY, "broken", name="Broken", jobs=5, error="HTTP 500"),
        F(ATS.WORKDAY, "sf.wd12.myworkdayjobs.com/External", name="Salesforce", jobs=1500, internships=12),
        F(ATS.WORKDAY, "sf.wd12.myworkdayjobs.com/Interns", name="Salesforce", jobs=40, internships=30),
    ]
    chosen = {(str(f.ats), f.slug) for f in bd.choose(found, curated)}
    assert chosen == {("ashby", "kalshi"), ("workday", "sf.wd12.myworkdayjobs.com/Interns")}


def test_robots_rules(monkeypatch):
    bd._robots_cache.clear()

    class F:
        async def get_text(self, url):
            return FetchResult(200, "User-agent: *\nAllow: /External/\nDisallow: /Research/\n")
    assert run(bd.robots_allows(F(), "x.wd1.myworkdayjobs.com", "External"))
    assert not run(bd.robots_allows(F(), "x.wd1.myworkdayjobs.com", "Research"))
