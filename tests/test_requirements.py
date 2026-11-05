"""Requirement extraction and profile checks. Phrases are taken verbatim from live postings."""

from datetime import UTC, datetime

import pytest

from internship_radar.models import ATS, Posting
from internship_radar.requirements import (
    Profile, check, current_standing, extract, extract_authorization, extract_clearance,
    extract_degrees, extract_fields, extract_gpa, extract_grad_window, extract_pay,
    extract_school_country, extract_standing, extract_terms, extract_weeks,
)

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def P(desc, title="Software Engineer Intern"):
    return Posting(ATS.GREENHOUSE, "1", "Acme", "greenhouse:acme", title, "https://x", description=desc)


# ---------------------------------------------------------------- graduation


@pytest.mark.parametrize("line,window", [
    ("university student graduating between September 2027 - July 2028 that is pursuing",
     ((2027, 9), (2028, 7))),
    ("Currently pursuing a B.S. or higher in Computer Science, graduating December 2027 or later",
     ((2027, 12), (2100, 12))),
    ("Bachelor's or Master's degree in Computer Science and graduating by June 2028",
     ((1900, 1), (2028, 6))),
    ("expected graduation date of Fall 2027 or Spring/Summer 2028", ((2027, 9), (2028, 8))),
    ("Pursuing a degree in a quantitative field, graduating 2027 (potentially 2028)",
     ((2027, 1), (2028, 12))),
    ("Must be planning on graduating in 2028. This should be your final internship", ((2028, 1), (2028, 12))),
    ("If you are graduating between December 2027 and July 2028, we invite you to apply for our "
     "Summer 2027 Academy", ((2027, 12), (2028, 7))),
])
def test_grad_window(line, window):
    assert extract_grad_window([line])[0] == window


def test_grad_window_ignores_non_graduation_lines():
    assert extract_grad_window(["Targeting a 2028 full-time start date"])[0] is None
    assert extract_grad_window(["secure a full time Graduate Quant Researcher position in 2028"])[0] is None


def test_grad_check_slack():
    req = extract(P("Graduating by June 2028"))
    assert check(req, Profile(graduation="2028-07"))[0].status == "ok"  # 1 month slack
    assert check(req, Profile(graduation="2028-12"))[0].status == "fail"


# ---------------------------------------------------------------- school after, standing


def test_return_to_school():
    desc = ("Summer 2027 internship\nMust be returning to school at the end of the internship to continue "
            "academic studies for at least one quarter or semester.")
    req = extract(P(desc, "Mechanical Engineer Intern (Summer 2027)"))
    assert req.school_after
    assert check(req, Profile(graduation="2028-05"))[0].status == "ok"
    assert check(req, Profile(graduation="2027-06"))[0].status == "fail"  # graduated before it ends


@pytest.mark.parametrize("line,expected,preferred", [
    ("Currently a junior pursuing a Bachelor's degree, with at least one more semester remaining",
     ["junior"], False),
    ("Rising seniors are preferred. Students must be onsite.", ["junior"], True),
    ("You must have completed at least the second year of your Undergraduate studies.",
     ["sophomore", "junior", "senior"], False),
    ("Communicate findings to senior management and other students", [], False),
    ("Capstone or senior design project experience as an undergraduate student", [], False),
])
def test_standing(line, expected, preferred):
    got, pref, _ = extract_standing([line])
    assert got == expected and (pref == preferred or not expected)


def test_current_standing():
    assert current_standing((2028, 5), NOW) == 3  # junior in Nov 2026
    assert current_standing((2027, 5), NOW) == 4
    assert current_standing((2029, 5), NOW) == 2
    assert current_standing((2030, 5), NOW) == 1


# ---------------------------------------------------------------- degree, major, GPA


@pytest.mark.parametrize("line,degrees", [
    ("Must be enrolled in a bachelor’s, master's or doctorate program in an engineering discipline",
     ["bs", "ms", "phd"]),
    ("Enrolled in a graduate program (Master's or PhD) in Computer Science, Computer Engineering",
     ["ms", "phd"]),
    ("Currently pursuing a B.S. in electrical or computer engineering", ["bs"]),
    ("Pursuing or received a degree in Engineering, preferably in Computer Science", []),
])
def test_degrees(line, degrees):
    assert extract_degrees([line])[0] == degrees


def test_fields_and_major_check():
    fams, related, _ = extract_fields(
        ["Currently pursuing a BA/BS degree in Accounting, Finance, or a related field"])
    assert fams == ["business"] and related
    req = extract(P("Currently pursuing a BA/BS degree in Accounting, Finance, or a related field"))
    c = check(req, Profile(majors=["computer science"]))
    assert c[0].status == "warn"  # "or related field": flagged, not excluded
    assert check(req, Profile(majors=["computer science"], strict_majors=True))[0].status == "fail"
    req = extract(P("Bachelor's degree in Electrical Engineering, Mechanical Engineering, or a related "
                    "technical field"))
    assert check(req, Profile(majors=["computer science"]))[0].status == "ok"  # technical field


@pytest.mark.parametrize("line,gpa,pref", [
    ("GPA of 3.0 or above", 3.0, False),
    ("A minimum GPA of 3.2", 3.2, False),
    ("3.5+ GPA preferred", 3.5, True),
    ("Cumulative grade point average of at least 3.3/4.0", 3.3, False),
])
def test_gpa(line, gpa, pref):
    got, p, _ = extract_gpa([line])
    assert got == gpa and p == pref


# ---------------------------------------------------------------- authorization, clearance, country


@pytest.mark.parametrize("line,auth", [
    ("Must be a U.S. Person due to required access to U.S. export-controlled information", "us_person"),
    ("U.S. Citizenship, Lawful Permanent Residency, or Refugee/Asylee Status Required", "us_person"),
    ("To conform to U.S. Government space technology export regulations, including the International "
     "Traffic in Arms Regulations (ITAR), Rocket Lab Employees must be a U.S. citizen, lawful permanent "
     "resident of the U.S., protected individual", "us_person"),
    ("Must be a U.S. citizen", "us_citizen"),
    ("Zipline is unable to sponsor work visas for applicants to this position.", "no_sponsorship"),
    ("For this internship, Optiver is unable to provide visa sponsorship", "no_sponsorship"),
    ("Must be authorized to work in the US without the need for sponsorship", "no_sponsorship"),
    # not requirements
    ("We consider applicants without regard to race, color, citizenship, or national origin", None),
    ("Company-sponsored sports teams and fitness events", None),
    ("This role may require an export license under U.S. export control laws without sponsorship for "
     "an export license", None),
])
def test_authorization(line, auth):
    from internship_radar.requirements import _lines
    assert extract_authorization(_lines(line))[0] == auth


def test_authorization_checks():
    req = extract(P("Must be a U.S. Person due to required access to export-controlled information"))
    assert check(req, Profile(citizenship="permanent_resident"))[0].status == "ok"
    assert check(req, Profile(citizenship="other"))[0].status == "fail"
    req = extract(P("Zipline is unable to sponsor work visas for applicants to this position."))
    assert check(req, Profile(citizenship="other", needs_sponsorship=False))[0].status == "ok"
    assert check(req, Profile(citizenship="other", needs_sponsorship=True))[0].status == "fail"


@pytest.mark.parametrize("line,level,pref", [
    ("Active US Security clearance, or eligibility and willingness to obtain a US Security clearance",
     "eligible", False),
    ("Must hold an active TS/SCI clearance", "active", False),
    ("U.S. clearance eligibility may be required depending on program", "eligible", True),
])
def test_clearance(line, level, pref):
    got, p, _ = extract_clearance([line])
    assert got == level and p == pref


def test_clearance_check():
    req = extract(P("Eligibility and willingness to obtain a US Security clearance"))
    assert check(req, Profile(clearance="none", citizenship="us_citizen"))[0].status == "ok"
    assert check(req, Profile(clearance="none", citizenship="other"))[0].status == "fail"


def test_school_country():
    assert extract_school_country(["degree in Computer Science from a university in Canada (required)"])[:2] \
        == ("CA", False)
    assert extract_school_country(["Candidates cannot be enrolled in an accredited U.S. university"])[:2] \
        == ("US", True)
    req = extract(P("Currently pursuing a degree in CS from a university in Canada (required)"))
    assert check(req, Profile(school_country="US"))[0].status == "fail"
    req = extract(P("Candidates cannot be enrolled in an accredited U.S. university"))
    assert check(req, Profile(school_country="US"))[0].status == "fail"


# ---------------------------------------------------------------- term, weeks, pay


@pytest.mark.parametrize("title,lines,terms", [
    ("Software Engineer Intern (Summer 2027)", [], [("summer", 2027)]),
    ("2027 Summer Intern, BS/MS, Scenes", [], [("summer", 2027)]),
    ("Winter 2027 Research Engineer Co-op", [], [("winter", 2027)]),
    ("Data Engineer Intern (2027)", [], [(None, 2027)]),
    ("Systems Intern", ["Able to work full-time for 12 consecutive weeks beginning May or June 2027"],
     [("summer", 2027)]),
    ("Systems Intern", ["expected graduation date of Fall 2027"], []),  # graduation, not term
])
def test_terms(title, lines, terms):
    assert extract_terms(title, lines) == terms


def test_term_check():
    req = extract(P("", "Software Engineer Intern (Fall 2027)"))
    assert check(req, Profile(terms=["summer-2027"]))[0].status == "fail"
    assert check(req, Profile(terms=["summer-2027", "fall-2027"]))[0].status == "ok"
    req = extract(P("", "Data Engineer Intern (2027)"))
    assert check(req, Profile(terms=["summer-2027"]))[0].status == "ok"


@pytest.mark.parametrize("line,weeks", [
    ("Our 10-week internship is your chance", (10, 10)),
    ("Ability to complete a 16-24-week in-person co-op in Costa Mesa", (16, 24)),
    ("Internships at Astranis typically last for twelve weeks", (12, 12)),
])
def test_weeks(line, weeks):
    assert extract_weeks([line]) == weeks


@pytest.mark.parametrize("line,pay", [
    ("The base pay for this position is $29.00 per hour.", (29.0, 29.0)),
    ("The hourly rate for this position is $47/hr for Undergrad, $53/hr for Graduate students", (47.0, 53.0)),
    ("The base salary for this position is $1,925 per week", (48.12, 48.12)),
    ("Compensation: $12.5K per month", (72.13, 72.13)),
    ("The salary range for this position is estimated to be $10,500/month", (60.59, 60.59)),
    ("The expected base pay range in the Toronto area is CAD $40 - CAD $45 per hour", None),
    ("Pay Range CA: $28.00 USD Hourly", (28.0, 28.0)),
    ("401(k) with company match up to $5,000 per year", None),
])
def test_pay(line, pay):
    assert extract_pay([line])[0] == pay


def test_pay_check_and_chips():
    req = extract(P("The base pay for this position is $29.00 per hour.\nHousing stipend provided"))
    assert check(req, Profile(min_hourly_pay=35))[0].status == "fail"
    assert "$29/hr" in req.chips() and "housing stipend" in req.chips()


def test_profile_validation():
    with pytest.raises(ValueError):
        Profile.from_dict({"graduation": "May 2028"})
    with pytest.raises(ValueError):
        Profile.from_dict({"terms": ["summer 2027"]})
    with pytest.raises(ValueError):
        Profile.from_dict({"gpaa": 3.5})
    assert Profile.from_dict({"graduation": "2028-05"}).grad_ym == (2028, 5)
