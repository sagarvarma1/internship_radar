"""Snapshot diffing, close/reopen tracking, per-user alert queueing, instant + nightly emails."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from internship_radar import alerts, db, mailer
from internship_radar.config import AlertConfig, PollConfig, Settings
from internship_radar.filters import FilterConfig, analyze
from internship_radar.models import ATS, Alert, Company, Posting, Remote

CO = Company("Acme", ATS.ASHBY, "acme", tags=("ai-lab",))
TAGS = {CO.key: CO.tags}


def post(i, title="Software Engineer Intern", desc="", locations=("San Francisco, CA",)):
    return Posting(ATS.ASHBY, str(i), "Acme", CO.key, title, f"https://jobs.example/{i}",
                   locations=list(locations), countries=["US"], remote=Remote.ONSITE, description=desc)


def snap(*posts):
    return [(p, analyze(p)) for p in posts]


@pytest.fixture(autouse=True)
def console_mail(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    mailer.SENT_LOG.clear()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.sync_registry(c, [CO])
    return c


@pytest.fixture
def settings(tmp_path):
    return Settings(filters=FilterConfig(), alerts=AlertConfig(batch_threshold=3), poll=PollConfig(),
                    db_path=tmp_path / "t.db", registry_path=Path("registry.yaml"),
                    discovered_path=tmp_path / "discovered.yaml",
                    site_url="https://radar.example", secure_cookies=True)


def add_user(conn, email, profile=None, **prefs):
    u, _ = db.get_or_create_user(conn, email, FilterConfig().to_dict())
    conn.execute("UPDATE users SET profile=? WHERE id=?", (json.dumps(profile or {}), u["id"]))
    for k, v in prefs.items():
        conn.execute(f"UPDATE users SET {k}=? WHERE id=?", (v, u["id"]))
    return u["id"]


def notify_pending(conn):
    return {r["uid"] for r in conn.execute("SELECT uid FROM postings WHERE notify_pending=1")}


# ---------------------------------------------------------------- storage


def test_baseline_does_not_notify_then_new_internships_do(conn):
    r = db.apply_snapshot(conn, CO.key, "e1", snap(post(1), post(2, "Account Executive")))
    assert r["baseline"] and r["new"] == 2 and notify_pending(conn) == set()
    r = db.apply_snapshot(conn, CO.key, "e2", snap(post(1), post(2, "Account Executive"), post(3),
                                                   post(4, "Sales Manager")))
    assert r["new"] == 2 and r["new_internships"] == 1
    assert notify_pending(conn) == {"ashby:acme:3"}


def test_dedupe_by_source_id(conn):
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(1)))
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    assert db.apply_snapshot(conn, CO.key, None, snap(post(1)))["new"] == 0


def test_closed_and_reopened(conn):
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2)))
    assert db.apply_snapshot(conn, CO.key, None, snap(post(1)))["closed"] == 1
    assert conn.execute("SELECT closed_at FROM postings WHERE uid='ashby:acme:2'").fetchone()[0]
    r = db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2)))
    assert r["reopened"] == 1 and r["new"] == 0
    assert conn.execute("SELECT closed_at FROM postings WHERE uid='ashby:acme:2'").fetchone()[0] is None


def test_company_health(conn):
    db.record_failure(conn, CO.key, "HTTP 500")
    db.record_failure(conn, CO.key, "HTTP 500")
    s = db.get_company_state(conn, CO.key)
    assert s["consecutive_failures"] == 2 and s["last_error"] == "HTTP 500"
    db.apply_snapshot(conn, CO.key, "e", snap(post(1)))
    s = db.get_company_state(conn, CO.key)
    assert s["consecutive_failures"] == 0 and s["last_success_at"] and s["open_internships"] == 1


def test_analysis_roundtrip(conn):
    p = post(1, "Software Engineer Intern (Summer 2027)", desc="Must be a U.S. citizen\nGPA of 3.0 or above")
    db.apply_snapshot(conn, CO.key, None, snap(p))
    row = conn.execute("SELECT * FROM postings").fetchone()
    a = db.row_to_analysis(row)
    assert a.is_internship and a.requirements.authorization == "us_citizen"
    assert a.requirements.gpa_min == 3.0 and a.requirements.terms == [("summer", 2027)]
    assert "US citizens only" in json.loads(row["chips"])


# ---------------------------------------------------------------- per-user alerts


def new_internship(conn, *posts):
    db.apply_snapshot(conn, CO.key, None, snap(post(0)))  # baseline
    db.apply_snapshot(conn, CO.key, None, snap(post(0), *posts))


def test_alerts_respect_each_users_profile(conn, settings):
    citizen = add_user(conn, "a@x.edu", {"citizenship": "us_citizen"})
    other = add_user(conn, "b@x.edu", {"citizenship": "other", "needs_sponsorship": True})
    new_internship(conn, post(1, desc="Must be a U.S. citizen"))
    assert alerts.queue_alerts(conn, settings, TAGS) == 1
    rows = conn.execute("SELECT user_id FROM user_alerts").fetchall()
    assert [r[0] for r in rows] == [citizen] and other != citizen
    assert notify_pending(conn) == set()


def test_instant_email_once(conn, settings):
    add_user(conn, "a@x.edu", nightly_digest=0)
    new_internship(conn, post(1), post(2))
    out = alerts.run(conn, settings, TAGS)
    assert out["queued"] == 2 and out["instant_emails"] == 2
    msgs = [m for m in mailer.SENT_LOG if m.to == "a@x.edu"]
    assert len(msgs) == 2 and msgs[0].subject.startswith("New internship: Acme")
    assert "https://jobs.example/" in msgs[0].text and "/unsubscribe/" in msgs[0].text
    assert msgs[0].headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert alerts.run(conn, settings, TAGS)["instant_emails"] == 0  # never twice


def test_burst_becomes_one_email(conn, settings):
    add_user(conn, "a@x.edu", nightly_digest=0)
    new_internship(conn, *[post(i) for i in range(1, 6)])
    assert alerts.run(conn, settings, TAGS)["instant_emails"] == 1
    assert mailer.SENT_LOG[-1].subject.startswith("5 new internships")


def test_all_emails_off(conn, settings):
    add_user(conn, "a@x.edu", instant_alerts=0, nightly_digest=0)
    new_internship(conn, post(1))
    out = alerts.run(conn, settings, TAGS)
    assert out["instant_emails"] == 0 and out["digests"] == 0 and mailer.SENT_LOG == []


def test_closed_posting_not_alerted(conn, settings):
    add_user(conn, "a@x.edu")
    new_internship(conn, post(1))
    alerts.queue_alerts(conn, settings, TAGS)
    db.apply_snapshot(conn, CO.key, None, snap(post(0)))  # 1 closed before sending
    assert alerts.send_instant(conn, settings) == 0


def test_digest_due():
    row = {"timezone": "America/Los_Angeles", "digest_hour": 20, "last_digest_at": None}
    # 2026-11-05 03:30 UTC = 19:30 PST (before) / 04:30 UTC = 20:30 PST (after)
    assert not alerts.digest_due(row, datetime(2026, 11, 5, 3, 30, tzinfo=UTC))
    assert alerts.digest_due(row, datetime(2026, 11, 5, 4, 30, tzinfo=UTC))
    row["last_digest_at"] = "2026-11-05T04:31:00+00:00"
    assert not alerts.digest_due(row, datetime(2026, 11, 5, 6, 0, tzinfo=UTC))  # same local day
    assert alerts.digest_due(row, datetime(2026, 11, 6, 4, 30, tzinfo=UTC))      # next evening


def test_nightly_digest_includes_instant_ones_and_runs_once(conn, settings):
    uid = add_user(conn, "a@x.edu", timezone="UTC", digest_hour=0)
    new_internship(conn, post(1), post(2))
    alerts.run(conn, settings, TAGS)  # 2 instant emails + the digest (hour 0 has passed)
    digests = [m for m in mailer.SENT_LOG if m.subject.startswith("Today's internships")]
    assert len(digests) == 1 and "2 new matches" in digests[0].subject
    assert alerts.send_digests(conn, settings) == 0  # once per day
    assert conn.execute("SELECT COUNT(*) FROM user_alerts WHERE user_id=? AND digested_at IS NULL",
                        (uid,)).fetchone()[0] == 0


def test_digest_skipped_when_nothing_new(conn, settings):
    add_user(conn, "a@x.edu", timezone="UTC", digest_hour=0)
    assert alerts.send_digests(conn, settings) == 0 and mailer.SENT_LOG == []


def test_one_bad_address_does_not_block_others(conn, settings, monkeypatch):
    add_user(conn, "bad@x.edu", nightly_digest=0)
    add_user(conn, "good@x.edu", nightly_digest=0)
    new_internship(conn, post(1))
    real = mailer.send

    def flaky(msg):
        if msg.to == "bad@x.edu":
            raise mailer.MailError("bounced")
        real(msg)
    monkeypatch.setattr(mailer, "send", flaky)
    out = alerts.run(conn, settings, TAGS)
    assert out["instant_emails"] == 1 and [m.to for m in mailer.SENT_LOG] == ["good@x.edu"]


def A(i, company="Acme"):
    return Alert(f"u{i}", company, f"SWE Intern {i}", ["San Francisco, CA"], Remote.HYBRID,
                 f"https://apply/{i}", 80, ["internship: title contains 'Intern'", "role: swe (+20)",
                                            "filtered: nothing"], datetime.now(UTC), ["Summer 2027"])


def test_formats():
    m = alerts.build_message("a@x.edu", [A(1)], "https://r.example", "https://r.example/unsubscribe/t")
    assert m.subject == "New internship: Acme - SWE Intern 1"
    for bit in ("Acme", "SWE Intern 1", "San Francisco, CA", "https://apply/1", "Summer 2027", "role: swe"):
        assert bit in m.text
    assert "filtered:" not in m.text and 'href="https://apply/1"' in m.html
    subject = alerts.subject_for([A(i, f"Co{i}") for i in range(6)])
    assert subject.startswith("6 new internships: Co0, Co1, Co2, Co3…")
    assert alerts.subject_for([A(1)], digest=True) == "Today's internships: 1 new match"


# ---------------------------------------------------------------- auth storage


def test_login_link_single_use_and_code(conn):
    token, code = db.create_login_token(conn, "a@x.edu", "1.2.3.4")
    assert db.consume_login_token(conn, token) == "a@x.edu"
    assert db.consume_login_token(conn, token) is None  # single use
    token, code = db.create_login_token(conn, "a@x.edu", "1.2.3.4")
    assert not db.consume_login_code(conn, "a@x.edu", "000000" if code != "000000" else "111111")
    assert db.consume_login_code(conn, "a@x.edu", code)
    assert not db.consume_login_code(conn, "a@x.edu", code)  # used


def test_code_attempts_limited(conn):
    _, code = db.create_login_token(conn, "a@x.edu", None)
    wrong = "123456" if code != "123456" else "654321"
    for _ in range(db.MAX_CODE_ATTEMPTS):
        assert not db.consume_login_code(conn, "a@x.edu", wrong)
    assert not db.consume_login_code(conn, "a@x.edu", code)  # locked after 5 misses


def test_delete_user_cascades(conn):
    uid = add_user(conn, "a@x.edu")
    db.create_session(conn, uid)
    db.set_status(conn, uid, "x", "applied")
    db.delete_user(conn, uid)
    for table in ("users", "sessions", "user_postings"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
