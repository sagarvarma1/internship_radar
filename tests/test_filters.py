"""Internship detection (including false positives), role classification, fit filters, scoring."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from internship_radar.ats import parse
from internship_radar.filters import FilterConfig, classify_roles, detect_internship, evaluate
from internship_radar.requirements import Profile
from internship_radar.models import ATS, Company, Posting, Remote

FIX = Path(__file__).parent / "fixtures"


def P(title, employment=None, desc="", locations=("San Francisco, CA",), countries=(),
      remote=Remote.UNKNOWN, dept=None, posted=None):
    return Posting(ATS.GREENHOUSE, "1", "Acme", "greenhouse:acme", title, "https://x",
                   department=dept, locations=list(locations), countries=list(countries),
                   remote=remote, employment_type=employment, description=desc, posted_at=posted)


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("title", [
    "Software Engineer Intern (Summer 2027)",
    "Software Engineering Internship",
    "2027 Summer Intern, BS/MS/PhD, ML Performance Engineering",
    "Machine Learning Interns",
    "Winter 2027 Research Engineer Co-op",
    "Software Engineer Co-Op",
    "Student Researcher, Gemini",
    "Working Student - Backend",
    "FPGA Associate (Summer 2027)",
    "Recruiting Intern",  # an internship *in* recruiting is still an internship
])
def test_internship_titles(title):
    assert detect_internship(P(title))[0], title


@pytest.mark.parametrize("title", [
    # real titles seen in live data that contain "intern" as a substring
    "AI Engineer, Internal Tools",
    "Associate Manager, Internal Audit",
    "Compliance, International Investigations Associate",
    "Senior Software Engineer - Query Engine, Database Internals - Elasticsearch",
    "Director, US International Tax Planning",
    "Internet Infrastructure Engineer",
    "Internist, Clinical Advisory",
    # jobs about interns, not internships
    "University Recruiter, Interns",
    "Internship Program Manager",
    "Recruiting Coordinator, University Internships",
    # new-grad / full-time roles with a season in the title
    "Sales Development Representative - Summer 2027 Graduates",
    "Campus Full Time 2027 - Quantitative Trader",
    "Senior Recruiter, University and Alternative Channels",
    "Software Engineer I, Entry-Level (Graduation Fall 2026 – Summer 2027)",
    "Senior Software Engineer",
    "Co-operative Partnerships Manager",
])
def test_not_internship_titles(title):
    assert not detect_internship(P(title))[0], title


def test_employment_type_detection():
    ok, why = detect_internship(P("Software Engineer, Mobile", employment="Intern"))
    assert ok and "employment type" in why[0]
    assert detect_internship(P("Analyst", employment="Summer Internship"))[0]
    assert not detect_internship(P("Software Engineer", employment="FullTime"))[0]
    assert not detect_internship(P("Software Engineer", employment="Full-time"))[0]
    # a board that labels a senior role "Intern" (seen live on Shield AI)
    assert not detect_internship(P("Senior Staff Engineer, Autonomy (R5837)", employment="Intern"))[0]


def test_description_alone_is_not_enough():
    benefits = "We run a great internship program and love mentoring. This internship program is..."
    assert not detect_internship(P("Software Engineer", desc=benefits))[0]
    # but an internship phrase plus a seasonal/student title is
    desc = "This is a 12-week internship based in our SF office."
    assert detect_internship(P("Software Engineer, Summer 2027 Student Program", desc=desc))[0]


def test_fixture_postings_detected_correctly():
    figma = parse(Company("Figma", ATS.GREENHOUSE, "figma"),
                  json.loads((FIX / "greenhouse_figma.json").read_text()))
    verdicts = {p.title: detect_internship(p)[0] for p in figma}
    assert verdicts["Data Engineer Intern (2027)"] is True
    assert verdicts["AI Engineer, Internal Tools"] is False


# ---------------------------------------------------------------- roles

@pytest.mark.parametrize("title,expected", [
    ("Software Engineer Intern", {"swe"}),
    ("Machine Learning Research Intern", {"ml", "research"}),
    ("Applied Scientist Intern", {"applied_science", "research"}),
    ("Infrastructure Engineer Intern", {"swe"}),
    ("CAD Engineer Intern (Spring 2027)", {"hardware"}),
    ("Propulsion Engineer Intern", {"hardware"}),
    ("Embedded Software Engineer Intern", {"swe", "hardware"}),
    ("Marketing Intern, AI Products", {"other"}),
    ("Product Designer Intern", {"other"}),
    ("Finance Intern", {"other"}),
])
def test_roles(title, expected):
    assert set(classify_roles(P(title))) == expected


# ---------------------------------------------------------------- fit filters & score

def test_good_match_scores_and_explains():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    m = evaluate(P("Machine Learning Intern", countries=["US"], remote=Remote.HYBRID,
                   posted=now - timedelta(days=1)),
                 FilterConfig(preferred_cities=["San Francisco"], preferred_tags=["ai-lab"]),
                 company_tags=("ai-lab",), now=now)
    assert m.is_internship and m.passes
    assert m.score == 40 + 25 + 10 + 5 + 5 + 5 + 5
    joined = " | ".join(m.reasons)
    for bit in ("internship", "role: ml", "US", "hybrid", "preferred city", "preferred company", "last 3 days"):
        assert bit in joined


def test_non_us_fails():
    m = evaluate(P("Software Engineer Intern", locations=["London, UK"], countries=["GB"]), FilterConfig())
    assert m.is_internship and not m.passes
    assert any("outside target countries" in r for r in m.reasons)


def test_us_detected_from_location_text():
    for loc in ["New York, NY", "Remote - United States", "Austin, Texas, USA", "Seattle"]:
        assert evaluate(P("SWE Intern", locations=[loc]), FilterConfig()).passes, loc


def test_unknown_location_configurable():
    p = P("SWE Intern", locations=[])
    assert evaluate(p, FilterConfig(allow_unknown_location=True)).passes
    assert not evaluate(p, FilterConfig(allow_unknown_location=False)).passes


def test_remote_preference():
    onsite = evaluate(P("SWE Intern", remote=Remote.ONSITE), FilterConfig())
    remote = evaluate(P("SWE Intern", remote=Remote.REMOTE), FilterConfig())
    assert onsite.score > remote.score and remote.passes
    assert not evaluate(P("SWE Intern", remote=Remote.REMOTE), FilterConfig(allow_remote=False)).passes


def test_role_filter():
    assert not evaluate(P("Marketing Intern"), FilterConfig()).passes
    assert evaluate(P("Marketing Intern"), FilterConfig(role_types=[])).passes


def prof(**kw):
    return FilterConfig(profile=Profile(**kw))


def test_graduation_window_against_profile():
    desc = "Candidates must be graduating between December 2027 and June 2028."
    assert evaluate(P("SWE Intern", desc=desc), prof(graduation="2028-05")).passes
    m = evaluate(P("SWE Intern", desc=desc), prof(graduation="2026-12"))
    assert not m.passes and any("graduation Dec 2027–Jun 2028" in r for r in m.reasons)
    # no stated window -> not filtered
    assert evaluate(P("SWE Intern"), prof(graduation="2026-12")).passes


@pytest.mark.parametrize("title,passes", [
    ("2027 Summer Intern, BS/MS/PhD, ML Performance", True),
    ("2027 Summer Intern, MS/PhD, Machine Learning Engineer", False),  # bs student
    ("PhD Research Intern", False),
    ("2027 Summer Intern, MBA, Strategic Finance Engineer", False),
    ("Software Engineer Intern (BS/MS)", True),
])
def test_degree_in_title(title, passes):
    assert evaluate(P(title), prof(degree_level="bs")).passes is passes


def test_degree_from_description_and_title_precedence():
    desc = "Enrolled in a graduate program (Master's or PhD) in Computer Science."
    assert not evaluate(P("Research Intern", desc=desc), prof(degree_level="bs")).passes
    assert evaluate(P("Research Intern", desc=desc), prof(degree_level="ms")).passes
    # title says BS/MS -> overrides the description
    assert evaluate(P("Research Intern (BS/MS)", desc=desc), prof(degree_level="bs")).passes


def test_requirements_unset_profile_never_filters():
    desc = ("Must be a U.S. citizen. Minimum GPA of 3.5. Graduating by June 2026. "
            "Enrolled in a PhD program.")
    m = evaluate(P("SWE Intern", desc=desc), FilterConfig())
    assert m.passes and m.chips  # requirements still extracted and shown


def test_requirement_bonus_and_warnings():
    desc = "Currently pursuing a B.S. in Computer Science\nGPA of 3.0 or above\nGraduating in 2028"
    m = evaluate(P("SWE Intern", desc=desc),
                 prof(graduation="2028-05", degree_level="bs", majors=["computer science"], gpa=3.6))
    assert m.passes and any(r.startswith("meets 4 stated requirements") for r in m.reasons)
    desc = "GPA of 3.5 or above preferred"
    m = evaluate(P("SWE Intern", desc=desc), prof(gpa=3.2))
    assert m.passes and any(r.startswith("⚠ GPA") for r in m.reasons)


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        FilterConfig.from_dict({"role_type": ["swe"]})  # typo
    cfg = FilterConfig.from_dict({"role_weights": {"swe": 30}})
    assert cfg.role_weights["swe"] == 30 and cfg.role_weights["ml"] == 25
