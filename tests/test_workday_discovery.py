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
    meta["full_at"] = (datetime.now(UTC) - timedelta(minutes=37)).isoformat()
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


def test_host_groups_rate_limit_workday_per_cluster():
    from internship_radar.fetch import group_interval, host_group
    assert host_group("nvidia.wd5.myworkdayjobs.com") == "wd5.myworkdayjobs.com"
    assert host_group("adobe.wd5.myworkdayjobs.com") == "wd5.myworkdayjobs.com"
    assert host_group("acme.wd1.myworkdayjobs.com") == "wd1.myworkdayjobs.com"
    assert host_group("boards-api.greenhouse.io") == "boards-api.greenhouse.io"
    assert host_group("api.lever.co") == "api.lever.co"
    assert group_interval("wd5.myworkdayjobs.com", 0.2) == 0.1
    assert group_interval("api.lever.co", 0.2) == 0.2


@pytest.mark.parametrize("label,ok", [
    ("Intern (Fixed Term)", True), ("Internship", True), ("Intern/Co-op", True),
    ("Student (Fixed Term)", True), ("Apprentice", True), ("Early Careers", True),
    ("Internal Audit Quality Assurance", False), ("International", False),
    ("Student Services", False), ("University of Arkansas for Medical Sciences", False),
    ("IT, Telecom & Internet", False),
])
def test_intern_facet_words(label, ok):
    assert workday.is_intern_facet(label) is ok


def test_large_backlog_is_stored_in_chunks_and_stays_baseline(conn, monkeypatch):
    monkeypatch.setattr(workday, "MAX_DETAILS_PER_POLL", 10)
    fake = FakeWorkday([job(i) for i in range(25)])
    r = run(workday.poll(conn, fake, NV))
    assert r["new"] == 10 and r["closed"] == 0
    assert conn.execute("SELECT baselined FROM companies").fetchone()[0] == 0  # still baselining
    run(workday.poll(conn, fake, NV))
    r = run(workday.poll(conn, fake, NV))
    assert r["new"] == 5
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 25
    assert conn.execute("SELECT SUM(notify_pending) FROM postings").fetchone()[0] == 0  # no emails
    assert conn.execute("SELECT baselined FROM companies").fetchone()[0] == 1
    fake.calls.clear()
    assert run(workday.poll(conn, fake, NV))["status"] == "not modified"


def test_wrong_facet_is_relearned_as_new_baseline(conn):
    fake = FakeWorkday([job(i) for i in range(3)])
    run(workday.poll(conn, fake, NV))
    bad = {"facet": {"param": "jobFamilyGroup", "id": "x", "label": "Internal Audit", "count": 9}}
    conn.execute("UPDATE companies SET meta=?", (json.dumps(bad),))
    conn.execute("DELETE FROM postings")
    r = run(workday.poll(conn, fake, NV))
    assert r["baseline"] and r["new"] == 3 and r["new_internships"] == 0


def test_location_facets_are_never_the_intern_facet():
    facets = [{"facetParameter": "remoteType", "values": [
                  {"descriptor": "On Campus/Onsite", "id": "a", "count": 559}]},
              {"facetParameter": "jobFamilyGroup", "values": [
                  {"descriptor": "Campus Recruiting", "id": "b", "count": 4}]}]
    assert workday.pick_intern_facet(facets)["id"] == "b"
    assert workday.pick_intern_facet(facets[:1]) is None


def test_truncated_listing_never_closes_postings(conn, monkeypatch):
    fake = FakeWorkday([job(i) for i in range(25)])
    run(workday.poll(conn, fake, NV))
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE closed_at IS NULL").fetchone()[0] == 25
    monkeypatch.setattr(workday, "MAX_PAGES", 1)  # listing now longer than we'll read
    fake.jobs.insert(0, job(99))
    r = run(workday.poll(conn, fake, NV))
    assert r["new"] == 1 and r["closed"] == 0  # the 6 past page 1 weren't seen, but aren't closed


def test_backlog_of_slow_tier_never_starves_minute_boards(tmp_path):
    from internship_radar.poller import overdue
    s = settings(tmp_path)
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    common = {"baselined": 1, "last_internship_at": None, "open_postings": 10,
              "consecutive_failures": 0, "last_error_at": None}
    minute = {**common, "key": "ashby:a", "source": "curated", "open_internships": 2,
              "last_polled_at": (now - timedelta(minutes=2)).isoformat()}
    slow = {**common, "key": "greenhouse:b", "source": "discovered", "open_internships": 0,
            "last_polled_at": (now - timedelta(minutes=20)).isoformat()}
    assert overdue(minute, s, now) > overdue(slow, s, now) > 0


def test_slow_tiers_are_spread_per_board(tmp_path):
    from internship_radar.poller import _interval
    s = settings(tmp_path)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    base = {"source": "discovered", "baselined": 1, "open_internships": 0, "last_internship_at": None,
            "open_postings": 10}
    spans = {_interval({**base, "key": f"greenhouse:{i}"}, s, now) for i in range(20)}
    assert len(spans) > 10 and all(timedelta(minutes=12) < x < timedelta(minutes=18) for x in spans)
    assert _interval({**base, "key": "x", "open_internships": 1}, s, now) == timedelta(minutes=1)
