"""End-to-end website flow with a real (temporary) database and the console mailer."""

import importlib
import re

import pytest
from fastapi.testclient import TestClient

from internship_radar import db, mailer
from internship_radar.filters import analyze
from internship_radar.models import ATS, Company, Posting, Remote

CO = Company("Acme", ATS.ASHBY, "acme", tags=("ai-lab",))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "web.db"))
    monkeypatch.setenv("RADAR_SITE_URL", "http://testserver")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    mailer.SENT_LOG.clear()
    conn = db.connect(tmp_path / "web.db")
    db.sync_registry(conn, [CO])
    posts = [
        Posting(ATS.ASHBY, "1", "Acme", CO.key, "Software Engineer Intern (Summer 2027)", "https://apply/1",
                locations=["San Francisco, CA"], countries=["US"], remote=Remote.ONSITE,
                description="Must be a U.S. citizen\nThe base pay for this position is $50 per hour."),
        Posting(ATS.ASHBY, "2", "Acme", CO.key, "Machine Learning Intern (Fall 2027)", "https://apply/2",
                locations=["New York, NY"], countries=["US"], remote=Remote.HYBRID),
    ]
    db.apply_snapshot(conn, CO.key, None, [(p, analyze(p)) for p in posts])
    conn.close()
    from internship_radar import web
    importlib.reload(web)
    return TestClient(web.app)


def sign_in(client, email="friend@school.edu"):
    r = client.post("/login", data={"email": email})
    assert r.status_code == 200 and "Check your email" in r.text
    msg = mailer.SENT_LOG[-1]
    link = re.search(r"http://testserver(/auth/\S+)", msg.text).group(1)
    code = re.search(r"code: (\d{6})", msg.subject).group(1)
    return link, code


def test_anonymous_home(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Every new tech internship" in r.text
    assert "Software Engineer Intern (Summer 2027)" in r.text and "Machine Learning Intern" in r.text
    assert "US citizens only" in r.text and "$50/hr" in r.text
    assert client.get("/settings", follow_redirects=False).headers["location"] == "/login"


def test_signup_by_link_then_settings_then_for_you(client):
    link, _ = sign_in(client)
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?welcome=1"
    assert "radar_session" in r.cookies or client.cookies.get("radar_session")
    assert "Welcome" in client.get("/settings?welcome=1").text
    # link is single-use
    assert client.get(link, follow_redirects=False).status_code == 400

    r = client.post("/settings", data={
        "welcome": "1", "graduation": "2028-05", "degree_level": "bs", "majors": "Computer Science",
        "gpa": "3.6", "citizenship": "other", "needs_sponsorship": "yes", "clearance": "none",
        "school_country": "US", "terms": ["summer-2027", "fall-2027"], "role_types": ["swe", "ml"],
        "location_scope": "us", "allow_remote": "on", "instant_alerts": "on", "nightly_digest": "on",
        "digest_hour": "20", "timezone": "America/New_York"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?tab=foryou"
    home = client.get("/?tab=foryou").text
    # citizenship-restricted role is excluded for this user; the other matches
    assert "Machine Learning Intern (Fall 2027)" in home
    assert "Software Engineer Intern (Summer 2027)" not in home
    all_tab = client.get("/?tab=all").text
    assert "s not a match" in all_tab and "US citizens only" in all_tab


def test_sign_in_with_code_and_status(client):
    _, code = sign_in(client, "b@school.edu")
    r = client.post("/auth/code", data={"email": "b@school.edu", "code": code}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/api/postings/ashby:acme:2/status", json={"status": "interested"})
    assert r.status_code == 200
    assert "Machine Learning Intern" in client.get("/?tab=saved").text
    assert client.post("/api/postings/ashby:acme:2/status", json={"status": "bogus"}).status_code == 400
    assert client.post("/api/postings/nope/status", json={"status": "applied"}).status_code == 404


def test_wrong_code_rejected(client):
    sign_in(client, "c@school.edu")
    r = client.post("/auth/code", data={"email": "c@school.edu", "code": "999999"}, follow_redirects=False)
    assert r.status_code in (303, 400)
    if r.status_code == 400:
        assert "Check the latest email" in r.text


def test_status_requires_sign_in(client):
    assert client.post("/api/postings/ashby:acme:1/status", json={"status": "applied"}).status_code == 401


def test_cross_site_post_blocked(client):
    r = client.post("/login", data={"email": "x@y.edu"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_bad_email_and_rate_limit(client):
    assert client.post("/login", data={"email": "not-an-email"}).status_code == 400
    for _ in range(7):
        r = client.post("/login", data={"email": "spam@school.edu"})
        assert r.status_code == 200  # same page either way: no probing
    assert sum(m.to == "spam@school.edu" for m in mailer.SENT_LOG) == 5


def test_unsubscribe_and_delete(client):
    link, _ = sign_in(client, "d@school.edu")
    client.get(link)
    from internship_radar import web
    c = db.connect(web.settings.db_path)
    token = c.execute("SELECT unsubscribe_token FROM users WHERE email='d@school.edu'").fetchone()[0]
    assert "Stop all emails?" in client.get(f"/unsubscribe/{token}").text
    assert "You're unsubscribed" in client.post(f"/unsubscribe/{token}").text
    u = c.execute("SELECT instant_alerts, nightly_digest FROM users WHERE email='d@school.edu'").fetchone()
    assert tuple(u) == (0, 0)
    r = client.post("/account/delete", follow_redirects=False)
    assert r.status_code == 303
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_companies_and_healthz(client):
    assert "Companies we watch" in client.get("/companies").text
    j = client.get("/healthz").json()
    assert j["ok"] and j["mail"] == "console"
