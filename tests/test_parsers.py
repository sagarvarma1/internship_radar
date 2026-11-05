"""ATS parsers against saved real responses (trimmed; see tests/fixtures/README.md)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from internship_radar.ats import board_url, html_to_text, parse
from internship_radar.models import ATS, Company, Remote

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_board_urls():
    assert board_url(Company("Ramp", ATS.ASHBY, "ramp")) == \
        "https://api.ashbyhq.com/posting-api/job-board/ramp"
    assert board_url(Company("Figma", ATS.GREENHOUSE, "figma")) == \
        "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true"
    assert board_url(Company("Palantir", ATS.LEVER, "palantir")) == \
        "https://api.lever.co/v0/postings/palantir?mode=json"
    assert board_url(Company("X", ATS.LEVER, "x", lever_region="eu")) == \
        "https://api.eu.lever.co/v0/postings/x?mode=json"
    with pytest.raises(ValueError):
        board_url(Company("Meta", ATS.UNSUPPORTED))


def test_ashby_parser():
    c = Company("Notion", ATS.ASHBY, "notion")
    posts = parse(c, load("ashby_notion.json"))
    assert len(posts) == 5
    p = posts[0]
    assert p.source is ATS.ASHBY
    assert p.source_id == "3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"
    assert p.uid == "ashby:notion:3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"
    assert p.title == "Software Engineer Intern (Summer 2027)"
    assert p.employment_type == "Intern"
    assert p.locations == ["San Francisco, California", "New York, New York"]
    assert "United States" in p.countries
    assert p.remote is Remote.HYBRID
    assert p.apply_url.startswith("https://jobs.ashbyhq.com/notion/")
    assert p.posted_at == datetime(2026, 8, 14, 19, 7, 13, 459000, tzinfo=UTC)
    assert p.description.startswith("WHO WE ARE")
    assert p.department == "Early Career"


def test_ashby_skips_unlisted_and_handles_missing_id():
    data = {"jobs": [
        {"title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/x/aaa"},
        {"title": "No id", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/x/bbb",
         "workplaceType": "Remote", "location": "Remote"},
    ]}
    posts = parse(Company("X", ATS.ASHBY, "x"), data)
    assert [p.title for p in posts] == ["No id"]
    assert posts[0].source_id == "bbb"  # falls back to the jobUrl's last segment
    assert posts[0].remote is Remote.REMOTE


def test_greenhouse_parser():
    c = Company("Figma", ATS.GREENHOUSE, "figma")
    posts = parse(c, load("greenhouse_figma.json"))
    assert len(posts) == 5
    intern = next(p for p in posts if p.source_id == "6178851004")
    assert intern.title == "Data Engineer Intern (2027)"
    assert intern.locations[:2] == ["San Francisco, CA", "New York, NY"]  # split on "•"
    assert intern.department == "Early Career"
    assert intern.apply_url == "https://boards.greenhouse.io/figma/jobs/6178851004?gh_jid=6178851004"
    assert intern.posted_at == datetime(2026, 9, 23, 19, 30, 27, tzinfo=UTC)
    # content is entity-encoded HTML; the description must be clean text
    assert "&lt;" not in intern.description and "<p>" not in intern.description
    assert intern.description.startswith("Figma is growing")


def test_greenhouse_metadata_remote_and_location_inference():
    data = {"jobs": [
        {"id": 1, "title": "A", "absolute_url": "u1", "location": {"name": "Remote - US"},
         "metadata": [{"name": "Location Type", "value": "Hybrid"}]},
        {"id": 2, "title": "B", "absolute_url": "u2", "location": {"name": "Remote | Remote - Canada"}},
        {"id": 3, "title": "C", "absolute_url": "u3", "location": {"name": "Austin, TX"}},
    ]}
    a, b, c = parse(Company("X", ATS.GREENHOUSE, "x"), data)
    assert a.remote is Remote.HYBRID  # explicit metadata wins over location text
    assert b.remote is Remote.REMOTE
    assert c.remote is Remote.UNKNOWN  # never assume on-site
    assert b.locations == ["Remote", "Remote - Canada"]


def test_lever_parser():
    c = Company("Palantir", ATS.LEVER, "palantir")
    posts = parse(c, load("lever_palantir.json"))
    assert len(posts) == 5
    p = posts[1]
    assert p.title == "Deployment Strategist, Internship - US Government"
    assert p.source_id == "a49d4181-a289-435a-b581-7f5af0497c8e"
    assert p.employment_type == "Internship"
    assert p.countries == ["US"]
    assert p.locations == ["Honolulu, HI"]
    assert p.remote is Remote.ONSITE  # workplaceType "on-site"
    assert p.posted_at == datetime(2025, 12, 11, 22, 31, 15, 446000, tzinfo=UTC)  # epoch ms
    assert p.apply_url == "https://jobs.lever.co/palantir/a49d4181-a289-435a-b581-7f5af0497c8e"
    assert p.description  # opening + lists + additional joined


def test_html_to_text():
    text = html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>a</li><li>b</li></ul>")
    assert [line for line in text.splitlines() if line] == ["Hello world", "a", "b"]
    assert html_to_text(None) == ""
