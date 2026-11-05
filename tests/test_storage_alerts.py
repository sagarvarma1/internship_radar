"""Snapshot diffing, close/reopen tracking, alert queueing, batching and dedupe."""

from datetime import UTC, datetime

import pytest

from internship_radar import alerts, db
from internship_radar.config import AlertConfig
from internship_radar.filters import FilterConfig, evaluate
from internship_radar.models import ATS, Alert, Company, Posting, Remote

CO = Company("Acme", ATS.ASHBY, "acme")


def post(i, title="Software Engineer Intern"):
    return Posting(ATS.ASHBY, str(i), "Acme", CO.key, title, f"https://jobs.example/{i}",
                   locations=["San Francisco, CA"], countries=["US"], remote=Remote.ONSITE)


def snap(*posts):
    cfg = FilterConfig()
    return [(p, evaluate(p, cfg)) for p in posts]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.sync_registry(c, [CO])
    return c


def pending(conn):
    return {r["uid"] for r in conn.execute("SELECT uid FROM postings WHERE alert_pending=1")}


def test_baseline_does_not_alert_then_new_postings_do(conn):
    r = db.apply_snapshot(conn, CO.key, "e1", snap(post(1), post(2, "Account Executive")))
    assert r["baseline"] and r["new"] == 2 and pending(conn) == set()
    r = db.apply_snapshot(conn, CO.key, "e2", snap(post(1), post(2, "Account Executive"), post(3)))
    assert not r["baseline"] and r["new"] == 1 and r["new_matches"] == 1
    assert pending(conn) == {"ashby:acme:3"}
    # non-matching new posting is stored but not queued
    db.apply_snapshot(conn, CO.key, "e3", snap(post(1), post(3), post(4, "Sales Manager")))
    assert pending(conn) == {"ashby:acme:3"}


def test_dedupe_by_source_id(conn):
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(1)))
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    r = db.apply_snapshot(conn, CO.key, None, snap(post(1)))
    assert r["new"] == 0


def test_closed_and_reopened(conn):
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2)))
    r = db.apply_snapshot(conn, CO.key, None, snap(post(1)))
    assert r["closed"] == 1
    row = conn.execute("SELECT closed_at FROM postings WHERE uid='ashby:acme:2'").fetchone()
    assert row["closed_at"] is not None
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


def test_alerts_send_once_per_channel(conn, capsys):
    db.apply_snapshot(conn, CO.key, None, snap(post(1)))
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2), post(3)))
    cfg = AlertConfig(channels=["console"], batch_threshold=3)
    status = alerts.run_alerts(conn, cfg)
    assert status["console"] == "sent 2"
    out = capsys.readouterr().out
    assert out.count("[alert]") == 2 and "https://jobs.example/2" in out
    assert pending(conn) == set()
    # second run: nothing re-sent
    assert alerts.run_alerts(conn, cfg)["console"] == "nothing pending"


def test_failed_channel_retries_without_resending_others(conn, monkeypatch):
    db.apply_snapshot(conn, CO.key, None, snap(post(1)))
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2)))
    cfg = AlertConfig(channels=["console", "discord"])
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    status = alerts.run_alerts(conn, cfg)
    assert status["console"] == "sent 1" and status["discord"].startswith("FAILED")
    assert pending(conn) == {"ashby:acme:2"}  # still owed to discord
    sent = []
    monkeypatch.setattr(alerts, "send_webhook", lambda env, payload: sent.append(payload))
    status = alerts.run_alerts(conn, cfg)
    assert status == {"console": "nothing pending", "discord": "sent 1"} and len(sent) == 1
    assert pending(conn) == set()


def test_closed_posting_not_alerted(conn):
    db.apply_snapshot(conn, CO.key, None, snap(post(1)))
    db.apply_snapshot(conn, CO.key, None, snap(post(1), post(2)))
    db.apply_snapshot(conn, CO.key, None, snap(post(1)))  # 2 closed before alerts ran
    assert alerts.run_alerts(conn, AlertConfig(channels=["console"]))["console"] == "nothing pending"


def A(i, company="Acme"):
    return Alert(f"u{i}", company, f"SWE Intern {i}", ["San Francisco, CA"], Remote.HYBRID,
                 f"https://apply/{i}", 80, ["internship: title contains 'Intern'", "role: swe (+20)",
                                            "filtered: nothing"], datetime.now(UTC))


def test_batching():
    uids = lambda bs: [[a.uid for a in b] for b in bs]
    assert uids(alerts.batches([A(1), A(2)], 3)) == [["u1"], ["u2"]]
    assert len(alerts.batches([A(i) for i in range(5)], 3)) == 1


def test_formats_include_required_fields():
    subject, text = alerts.format_text([A(1)])
    assert subject == "New internship: Acme - SWE Intern 1"
    for bit in ("Acme", "SWE Intern 1", "San Francisco, CA", "https://apply/1", "role: swe"):
        assert bit in text
    assert "filtered:" not in text
    html = alerts.format_html([A(1)])
    assert 'href="https://apply/1"' in html
    a = A(2)
    a.chips = ["Summer 2027", "grad by Jun 2028", "GPA ≥ 3"]
    _, text = alerts.format_text([a])
    assert "Requirements: Summer 2027 · grad by Jun 2028 · GPA ≥ 3" in text
    assert "Summer 2027 · grad by Jun 2028" in alerts.format_html([a])
    subject, _ = alerts.format_text([A(i, f"Co{i}") for i in range(6)])
    assert subject.startswith("6 new internships: Co0, Co1, Co2, Co3…")
