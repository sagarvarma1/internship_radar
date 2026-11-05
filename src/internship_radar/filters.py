"""Internship detection, fit filters and an explainable relevance score."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Match, Posting, Remote

# ---------------------------------------------------------------- config


@dataclass
class FilterConfig:
    # Role types you want. Options: swe, ml, research, applied_science, data, hardware
    role_types: list[str] = field(default_factory=lambda: ["swe", "ml", "research", "applied_science"])
    # Country codes to accept. Empty list = anywhere.
    countries: list[str] = field(default_factory=lambda: ["US"])
    allow_remote: bool = True
    allow_unknown_location: bool = True
    preferred_cities: list[str] = field(default_factory=list)
    # Your graduation year(s). Postings that state a window excluding all of these fail.
    graduation_years: list[int] = field(default_factory=list)
    # Your degree level(s). Postings that only accept other levels (e.g. "MS/PhD", "MBA") fail.
    degree_levels: list[str] = field(default_factory=lambda: ["bs", "ms"])
    preferred_tags: list[str] = field(default_factory=list)
    min_score: float = 0.0
    role_weights: dict[str, float] = field(
        default_factory=lambda: {
            "ml": 25, "research": 22, "applied_science": 22, "swe": 20, "data": 12, "hardware": 10,
        }
    )

    @classmethod
    def from_dict(cls, d: dict | None) -> FilterConfig:
        d = dict(d or {})
        weights = {**cls().role_weights, **(d.pop("role_weights", None) or {})}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        unknown = set(d) - set(known)
        if unknown:
            raise ValueError(f"unknown filter config keys: {sorted(unknown)}")
        return cls(**known, role_weights=weights)


# ---------------------------------------------------------------- internship detection

# Word-boundary "intern" family. \b stops "internal", "international", "internet", "internist"
# from matching because those continue with more letters.
INTERN_TITLE = re.compile(r"\bintern(?:s|ship|ships)?\b", re.I)
COOP_TITLE = re.compile(r"\bco-?op\b", re.I)
STUDENT_TITLE = re.compile(
    r"\b(student researcher|student research(?:er)?|working student|student engineer|"
    r"student developer|summer student|student program)\b",
    re.I,
)
TERM_TITLE = re.compile(r"\b(summer|fall|autumn|winter|spring)\s*(?:'|20)?(\d{2})\b", re.I)
# Titles that mention interns but are full-time jobs *about* interns, e.g.
# "University Recruiter, Interns", "Internship Program Manager". A singular "Intern" as the role
# noun ("Recruiting Intern") is still an internship.
INTERN_ADJACENT_JOB = re.compile(
    r"\bintern(ship)?s?\s+programs?\s+(manager|coordinator|lead|specialist|director)\b|"
    r"\b(recruit\w*|coordinator|manager|sourcer|specialist|partner|director|lead)\b.*"
    r"\b(interns|internships|intern(ship)? programs?)\b",
    re.I,
)
INTERN_EMPLOYMENT = re.compile(r"\b(intern(ship)?|co-?op|student|temporary student)\b", re.I)
DESC_STRONG = re.compile(
    r"\b(this (is an? )?(paid )?internship|our internship|\d{1,2}[- ](?:to[- ]\d{1,2}[- ])?week"
    r" internship|internship (program|position|role) (is|runs|will)|as an intern\b|"
    r"intern(ship)? start dates?)",
    re.I,
)
FULL_TIME_TITLE = re.compile(
    r"\b(new grad\w*|graduates?|university grad\w*|early career|full[- ]time|campus full|"
    r"rotational|associate program|analyst program)\b", re.I)


def detect_internship(p: Posting) -> tuple[bool, list[str]]:
    title = p.title
    reasons: list[str] = []

    if INTERN_TITLE.search(title) or COOP_TITLE.search(title) or STUDENT_TITLE.search(title):
        if INTERN_ADJACENT_JOB.search(title):
            return False, [f"title is a job about interns ('{title}')"]
        hit = (INTERN_TITLE.search(title) or COOP_TITLE.search(title) or STUDENT_TITLE.search(title))
        reasons.append(f"title contains '{hit.group(0)}'")
        return True, reasons

    if p.employment_type and INTERN_EMPLOYMENT.search(p.employment_type):
        return True, [f"employment type is '{p.employment_type}'"]

    # "FPGA Associate (Summer 2027)": a season + year in the title, with no sign it's a
    # full-time/new-grad role, is an internship-style term position.
    term = TERM_TITLE.search(title)
    if term and not FULL_TIME_TITLE.search(title):
        return True, [f"title names a term ('{term.group(0)}')"]

    # Description-only evidence is noisy (benefits blurbs mention internship programs), so it
    # needs an internship-specific phrase AND a student-ish or seasonal title.
    term = TERM_TITLE.search(title)
    if DESC_STRONG.search(p.description or "") and (term or re.search(r"\bstudent|university\b", title, re.I)):
        return True, [f"description says '{DESC_STRONG.search(p.description).group(0)}'"
                      + (f" and title mentions '{term.group(0)}'" if term else "")]
    return False, []


# ---------------------------------------------------------------- role types

ROLE_PATTERNS: dict[str, re.Pattern] = {
    "ml": re.compile(
        r"\b(machine learning|ml|ai|artificial intelligence|deep learning|llms?|nlp|"
        r"computer vision|perception|reinforcement learning|rl|inference|training|"
        r"model(s|ing)?|genai|generative)\b", re.I),
    "research": re.compile(r"\b(research(er)?|scientist)\b", re.I),
    "applied_science": re.compile(r"\b(applied (scien\w+|research)|quantitative research)\b", re.I),
    "swe": re.compile(
        r"\b(software|swe|developer|engineer(ing)?|backend|back-end|frontend|front-end|"
        r"full[- ]?stack|infrastructure|platform engineer\w*|distributed systems|mobile|ios|android|"
        r"devops|sre|site reliability|security engineer|compilers?|product engineer|web|cloud|"
        r"forward deployed)\b", re.I),
    "data": re.compile(r"\b(data scien\w+|data engineer\w*|analytics|data analyst)\b", re.I),
    "hardware": re.compile(
        r"\b(hardware|electrical|mechanical|firmware|embedded|asic|fpga|robotics|avionics|cad|"
        r"propulsion|fluids?|thermal|structures?|structural|manufacturing|materials|quality|test|"
        r"rf|antenna|power|gnc|guidance|aerodynamics?|aerospace|mechatronics|process|production|"
        r"industrial|optical|photonics|silicon|chip|pcb|vehicle|launch|ground systems|integration|"
        r"reliability engineer|hitl|controls?|actuator|battery|motor|welding|composites?)\b", re.I),
}
NON_TECH = re.compile(
    r"\b(marketing|sales|account (executive|manager)|finance|accounting|legal|counsel|recruit\w*|"
    r"people|hr|human resources|communications|policy|design(er)?|ux|brand|content|"
    r"operations|business development|partnerships|customer|support|"
    r"office|facilities|event|social media|treasury|tax|audit|strategy)\b", re.I)


def classify_roles(p: Posting) -> list[str]:
    title = p.title
    scope = f"{title} | {p.department or ''} | {p.team or ''}"
    roles = [r for r, pat in ROLE_PATTERNS.items() if pat.search(title)]
    if not roles:
        roles = [r for r, pat in ROLE_PATTERNS.items() if r != "ml" and pat.search(scope)]
    # "Marketing Intern, AI Products" is not an ML role.
    if NON_TECH.search(title) and not re.search(r"\b(engineer|scientist|developer|research)", title, re.I):
        return ["other"]
    # A bare "engineer" in a hardware context is hardware, not swe.
    if "hardware" in roles and "swe" in roles and not re.search(
        r"\b(software|swe|backend|frontend|full[- ]?stack|firmware|embedded|developer|web|cloud|"
        r"infrastructure|platform|distributed)\b", title, re.I
    ):
        roles.remove("swe")
    return roles or ["other"]


# ---------------------------------------------------------------- location

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_PATTERN = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|us|" + "|".join(US_STATES) + r")\b|"
    r"\b(san francisco|new york|nyc|seattle|boston|cambridge, ma|palo alto|mountain view|"
    r"menlo park|sunnyvale|san jose|los angeles|austin|chicago|denver|washington|atlanta|"
    r"pittsburgh|redmond|bellevue|san mateo|redwood city|south san francisco|oakland|"
    r"berkeley|cupertino|santa clara|san diego|irvine|miami|philadelphia|salt lake|"
    r"portland|bay area|silicon valley)\b",
    re.I,
)
US_CODES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}


def location_verdict(p: Posting, cfg: FilterConfig) -> tuple[bool | None, str]:
    """True = in an accepted country, False = clearly not, None = unknown."""
    if not cfg.countries:
        return True, "any location accepted"
    want_us = any(c.upper() in US_CODES for c in cfg.countries)
    others = {c.upper() for c in cfg.countries} - US_CODES
    countries = {c.upper() for c in p.countries}
    if countries:
        if want_us and countries & US_CODES:
            return True, "located in the US"
        if countries & others:
            return True, f"located in {', '.join(sorted(countries & others))}"
    text = " / ".join(p.locations)
    if want_us and US_PATTERN.search(text):
        return True, f"US location ({text[:60]})"
    if any(o.lower() in text.lower() for o in others):
        return True, f"location {text[:60]}"
    if countries or (text and text.lower() not in ("remote", "anywhere", "global")):
        return False, f"outside target countries ({text[:60] or ', '.join(countries)})"
    return None, "location unclear"


# ---------------------------------------------------------------- grad year / degree

GRAD_WINDOW = re.compile(r"(graduat\w*|class of|degree completion|completing (your|a) degree)", re.I)
YEAR = re.compile(r"\b(20[2-3]\d)\b")
PHD_ONLY = re.compile(
    r"\b(currently (enrolled|pursuing) (in )?an? ph\.?d|must be (enrolled in|pursuing) a ph\.?d|"
    r"ph\.?d\.? (students?|candidates?) only|enrolled in a ph\.?d\.? program)", re.I)
INCLUSIVE_DEGREE = re.compile(r"\b(bachelor'?s|b\.?s\.?|undergraduate|master'?s|m\.?s\.?)\b", re.I)
DEGREE_TOKENS = {
    "bs": re.compile(r"\b(b\.?s\.?|b\.?a\.?|bachelor'?s?|undergrad(uate)?s?)\b", re.I),
    "ms": re.compile(r"\b(m\.?s\.?|master'?s?|meng|graduate)\b", re.I),
    "phd": re.compile(r"\bph\.?d\.?\b", re.I),
    "mba": re.compile(r"\bmba\b", re.I),
}


def degrees_stated(p: Posting) -> tuple[set[str], str]:
    """Degree levels a posting is restricted to, from the title (e.g. "BS/MS/PhD") or a
    PhD-only phrase in the description. Empty set = not restricted / not stated."""
    # Only look at a title segment that looks like a degree list, e.g. ", MS/PhD," or "(PhD)".
    for seg in re.split(r"[,()\-–|]", p.title):
        found = {lvl for lvl, pat in DEGREE_TOKENS.items() if pat.search(seg)}
        words = re.sub(r"[\s/&+]|and|or", "", seg, flags=re.I)
        # segment made only of degree tokens -> it's an explicit degree list
        if found and len(re.sub(r"(?i)b\.?s\.?|b\.?a\.?|m\.?s\.?|ph\.?d\.?|mba|bachelors?|masters?|undergrad(uate)?|meng", "", words)) <= 2:
            return found, f"title says {seg.strip()}"
    # "PhD Research Intern": a PhD/MBA token anywhere, with no BS/MS mentioned, restricts it.
    whole = {lvl for lvl, pat in DEGREE_TOKENS.items() if pat.search(p.title)}
    if whole and whole <= {"phd", "mba"}:
        return whole, f"title says {'/'.join(sorted(whole)).upper()}"
    m = PHD_ONLY.search(p.description or "")
    if m:
        around = p.description[max(0, m.start() - 150): m.end() + 150]
        if not INCLUSIVE_DEGREE.search(around):
            return {"phd"}, f"description says '{m.group(0)}'"
    return set(), ""


def grad_years_stated(text: str) -> list[int]:
    years: set[int] = set()
    for m in GRAD_WINDOW.finditer(text or ""):
        window = text[m.start(): m.end() + 120]
        years.update(int(y) for y in YEAR.findall(window))
    return sorted(years)


# ---------------------------------------------------------------- scoring


def evaluate(p: Posting, cfg: FilterConfig, company_tags: tuple[str, ...] = (),
             now: datetime | None = None) -> Match:
    now = now or datetime.now(UTC)
    is_intern, reasons = detect_internship(p)
    roles = classify_roles(p)
    if not is_intern:
        return Match(False, 0.0, reasons or ["not an internship"], roles, False)

    score = 40.0
    reasons = [f"internship: {reasons[0]}"]
    passes = True
    fails: list[str] = []

    wanted = [r for r in roles if r in cfg.role_types]
    if wanted:
        best = max(wanted, key=lambda r: cfg.role_weights.get(r, 10))
        score += cfg.role_weights.get(best, 10)
        reasons.append(f"role: {', '.join(wanted)} (+{cfg.role_weights.get(best, 10):g})")
    elif cfg.role_types:
        passes = False
        fails.append(f"role type {', '.join(roles)} not in {cfg.role_types}")

    loc_ok, loc_why = location_verdict(p, cfg)
    if loc_ok is True:
        score += 10
        reasons.append(f"{loc_why} (+10)")
    elif loc_ok is None:
        if cfg.allow_unknown_location:
            reasons.append(loc_why)
        else:
            passes = False
            fails.append(loc_why)
    else:
        passes = False
        fails.append(loc_why)

    if p.remote in (Remote.ONSITE, Remote.HYBRID):
        score += 5
        reasons.append(f"{p.remote} (+5)")
    elif p.remote is Remote.REMOTE:
        if not cfg.allow_remote:
            passes = False
            fails.append("fully remote")
        else:
            reasons.append("fully remote")

    loc_text = " ".join(p.locations).lower()
    cities = [c for c in cfg.preferred_cities if c.lower() in loc_text]
    if cities:
        score += 5
        reasons.append(f"preferred city {cities[0]} (+5)")

    desc = p.description or ""
    if cfg.graduation_years:
        stated = grad_years_stated(desc)
        if stated:
            lo, hi = min(stated), max(stated)
            mine = [y for y in cfg.graduation_years if lo <= y <= hi]
            if mine:
                score += 5
                reasons.append(f"grad year {mine[0]} fits stated {lo}" + (f"–{hi}" if hi != lo else "") + " (+5)")
            else:
                passes = False
                fails.append(f"stated graduation years {lo}–{hi} exclude {cfg.graduation_years}")

    if cfg.degree_levels:
        stated, where = degrees_stated(p)
        if stated:
            ok = stated & set(cfg.degree_levels)
            if ok:
                reasons.append(f"degree: {where}")
            else:
                passes = False
                fails.append(f"degree {'/'.join(sorted(stated))} only ({where})")

    term = TERM_TITLE.search(p.title)
    if term:
        reasons.append(f"term: {term.group(0)}")

    tags = [t for t in company_tags if t in cfg.preferred_tags]
    if tags:
        score += 5
        reasons.append(f"preferred company ({tags[0]}) (+5)")

    if p.posted_at and (now - p.posted_at).days <= 3:
        score += 5
        reasons.append("posted in the last 3 days (+5)")

    if passes and score < cfg.min_score:
        passes = False
        fails.append(f"score {score:g} below min {cfg.min_score:g}")

    return Match(True, score, reasons + [f"filtered: {f}" for f in fails], roles, passes)
