"""Eligibility requirements stated in a posting, and checks against your profile.

Extraction covers what internship applications actually ask about (patterns derived from ~1,000
live internship postings): graduation window, school remaining after the internship, year in
school, degree level, major, GPA, work authorization / sponsorship, security clearance, the
country your school is in, internship term, duration and pay.

Every check returns ok / fail / warn with the posting's own words, so a filtered posting always
says exactly which requirement excluded it. Requirements a posting doesn't state never exclude it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .models import Posting

# ---------------------------------------------------------------- profile


@dataclass
class Profile:
    graduation: str | None = None  # expected graduation "YYYY-MM", e.g. "2028-05"
    degree_level: str | None = None  # bs | ms | phd | mba
    majors: list[str] = field(default_factory=list)  # e.g. ["computer science", "mathematics"]
    gpa: float | None = None
    citizenship: str | None = None  # us_citizen | permanent_resident | other
    needs_sponsorship: bool | None = None  # now or in future (H-1B etc.)
    clearance: str | None = None  # none | eligible | active ("eligible" = could obtain one)
    school_country: str | None = None  # ISO code of your university's country, e.g. "US"
    terms: list[str] = field(default_factory=list)  # e.g. ["summer-2027", "fall-2027"]
    min_hourly_pay: float | None = None  # USD; postings stating less are excluded
    strict_majors: bool = False  # True: fail when listed majors don't include yours

    @classmethod
    def from_dict(cls, d: dict | None) -> Profile:
        d = dict(d or {})
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown profile keys: {sorted(unknown)}")
        if d.get("graduation") is not None:
            d["graduation"] = str(d["graduation"])
            _parse_ym(d["graduation"])  # validate early
        if d.get("degree_level") and d["degree_level"] not in DEGREES:
            raise ValueError(f"degree_level must be one of {sorted(DEGREES)}")
        if d.get("citizenship") and d["citizenship"] not in ("us_citizen", "permanent_resident", "other"):
            raise ValueError("citizenship must be us_citizen, permanent_resident or other")
        if d.get("clearance") and d["clearance"] not in ("none", "eligible", "active"):
            raise ValueError("clearance must be none, eligible or active")
        for t in d.get("terms") or []:
            _parse_term_key(t)
        return cls(**d)

    @property
    def grad_ym(self) -> tuple[int, int] | None:
        return _parse_ym(self.graduation) if self.graduation else None


DEGREES = {"bs", "ms", "phd", "mba"}


def _parse_ym(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(20\d\d)-(0?[1-9]|1[0-2])", s.strip())
    if not m:
        raise ValueError(f"graduation must look like 2028-05, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def _parse_term_key(s: str) -> tuple[str, int]:
    m = re.fullmatch(r"(summer|fall|winter|spring)-(20\d\d)", s.strip().lower())
    if not m:
        raise ValueError(f"terms look like summer-2027, got {s!r}")
    return m.group(1), int(m.group(2))


# ---------------------------------------------------------------- extracted requirements


@dataclass
class Requirements:
    # (see school_country_not below)
    grad_window: tuple[tuple[int, int], tuple[int, int]] | None = None  # (lo, hi) year-month
    grad_text: str | None = None
    school_after: bool = False  # must have school remaining after the internship
    school_after_text: str | None = None
    standings: list[str] = field(default_factory=list)  # allowed current standing
    standings_preferred: bool = False
    standing_text: str | None = None
    degrees: list[str] = field(default_factory=list)
    degree_text: str | None = None
    fields: list[str] = field(default_factory=list)  # field families
    fields_related_ok: bool = False
    fields_text: str | None = None
    gpa_min: float | None = None
    gpa_preferred: bool = False
    gpa_text: str | None = None
    authorization: str | None = None  # us_citizen | us_person | no_sponsorship
    authorization_text: str | None = None
    clearance: str | None = None  # active | eligible
    clearance_preferred: bool = False
    clearance_text: str | None = None
    school_country: str | None = None
    school_country_not: str | None = None  # "cannot be enrolled in a U.S. university"
    school_country_text: str | None = None
    terms: list[tuple[str | None, int]] = field(default_factory=list)  # (season or None, year)
    weeks: tuple[int, int] | None = None
    pay_hourly: tuple[float, float] | None = None  # USD/hour
    pay_text: str | None = None
    housing: bool = False

    def to_json(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, False, [], ())}

    def chips(self) -> list[str]:
        """Short labels for the UI and alerts."""
        out = []
        if self.terms:
            out.append(", ".join(f"{(s or '').title()} {y}".strip() for s, y in self.terms))
        if self.grad_window:
            out.append(f"grad {_fmt_window(self.grad_window)}")
        if self.degrees:
            out.append("/".join(d.upper() if d != "phd" else "PhD" for d in sorted(self.degrees, key=_deg_order)))
        if self.gpa_min:
            out.append(f"GPA ≥ {self.gpa_min:g}" + (" pref" if self.gpa_preferred else ""))
        if self.authorization:
            out.append({"us_citizen": "US citizens only", "us_person": "US persons only",
                        "no_sponsorship": "no visa sponsorship"}[self.authorization])
        if self.clearance:
            out.append(f"clearance {'(active)' if self.clearance == 'active' else 'eligible'}"
                       + (" pref" if self.clearance_preferred else ""))
        if self.standings:
            out.append("/".join(self.standings) + (" pref" if self.standings_preferred else ""))
        if self.school_after:
            out.append("must return to school")
        if self.school_country:
            out.append(f"school in {self.school_country}")
        if self.school_country_not:
            out.append(f"school not in {self.school_country_not}")
        if self.weeks:
            lo, hi = self.weeks
            out.append(f"{lo}–{hi} wk" if hi != lo else f"{lo} wk")
        if self.pay_hourly:
            lo, hi = self.pay_hourly
            out.append(f"${lo:.0f}/hr" if abs(hi - lo) < 1 else f"${lo:.0f}–{hi:.0f}/hr")
        if self.housing:
            out.append("housing stipend")
        return out


def _deg_order(d: str) -> int:
    return ["bs", "ms", "mba", "phd"].index(d) if d in ("bs", "ms", "mba", "phd") else 9


MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
# season -> (first month, last month) as a graduation time
SEASON_GRAD = {"spring": (3, 6), "summer": (6, 8), "fall": (9, 12), "autumn": (9, 12), "winter": (12, 12)}


def _fmt_ym(ym: tuple[int, int]) -> str:
    return f"{list(MONTHS)[ym[1] - 1][:3].title()} {ym[0]}"


def _fmt_window(w) -> str:
    lo, hi = w
    if lo == (1900, 1):
        return f"by {_fmt_ym(hi)}"
    if hi == (2100, 12):
        return f"{_fmt_ym(lo)} or later"
    return _fmt_ym(lo) if lo == hi else f"{_fmt_ym(lo)}–{_fmt_ym(hi)}"


# ---------------------------------------------------------------- text helpers

EEO = re.compile(
    r"discriminat|without regard|regardless of|equal (employment )?opportunit|protected (veteran|class|"
    r"characteristic)|on the basis of|national origin|affirmative action|e-verify|reasonable accommodation",
    re.I)


def _lines(text: str) -> list[str]:
    """Split into bullet/line units. Never split on "." (U.S., 3.0, Ph.D.)."""
    out = []
    for line in re.split(r"\n+|\s[•·▪]\s", text or ""):
        line = line.strip(" -*•\t")
        if line and not EEO.search(line):
            out.append(line)
    return out


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


PREFERRED = re.compile(r"\b(prefer\w*|ideally|nice to have|bonus|a plus|desired|strongly encouraged)\b", re.I)

# ---------------------------------------------------------------- graduation

DATE_TOKEN = re.compile(
    r"\b(?:(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|"
    r"(?P<season>spring|summer|fall|autumn|winter)(?:/(?:spring|summer|fall|autumn|winter))*)?"
    r"[\s,']*(?:of\s+)?(?P<year>20[2-3]\d)\b",
    re.I)
GRAD_CUE = re.compile(r"graduat\w*|class of|degree (completion|conferral)|complet\w+ (your|their|a) degree", re.I)


def _token_range(m: re.Match) -> tuple[tuple[int, int], tuple[int, int]]:
    y = int(m.group("year"))
    if m.group("mon"):
        mon = MONTHS[m.group("mon").lower()[:4] if m.group("mon").lower().startswith("sept") else m.group("mon").lower()[:3]]
        return (y, mon), (y, mon)
    if m.group("season"):
        seasons = re.findall(r"spring|summer|fall|autumn|winter", m.group(0), re.I)
        months = [SEASON_GRAD[s.lower()] for s in seasons]
        return (y, min(a for a, _ in months)), (y, max(b for _, b in months))
    return (y, 1), (y, 12)


def extract_grad_window(lines: list[str]):
    for line in lines:
        cue = GRAD_CUE.search(line)
        if not cue:
            continue
        seg = line[cue.start(): cue.end() + 140]
        if re.search(r"\b(full[- ]time start|start date|graduate (position|program|role|quant|software))", seg, re.I):
            continue
        toks = list(DATE_TOKEN.finditer(seg))
        if not toks:
            continue
        # keep the first date plus dates chained to it by connectors ("and", "or", "to", "-",
        # "(potentially 2028)"); later unrelated dates ("apply for Summer 2027") are ignored
        chain = [toks[0]]
        for t in toks[1:]:
            gap = seg[chain[-1].end(): t.start()]
            if len(gap) <= 16 and re.fullmatch(r"[\s,/()\-–—]*(and|or|to|through|until|thru|potentially|"
                                               r"possibly|maybe|and/or)?[\s,/()\-–—]*(potentially|possibly)?\s*", gap, re.I):
                chain.append(t)
            else:
                break
        toks = chain
        ranges = [_token_range(t) for t in toks]
        lo = min(r[0] for r in ranges)
        hi = max(r[1] for r in ranges)
        before = seg[: toks[0].start()].lower()
        after = seg[toks[-1].end(): toks[-1].end() + 25].lower()
        if re.search(r"\b(by|before|no later than|prior to|on or before)\s*$", before.strip() + " ") or \
                re.search(r"\b(by|before|no later than|prior to)\b[^0-9]{0,12}$", before):
            lo = (1900, 1)
        elif re.search(r"^\W*(or|and) (later|after|beyond)|^\W*\+|^\W*onwards?", after) or \
                re.search(r"\b(after|no earlier than|on or after|or later than)\b[^0-9]{0,12}$", before):
            hi = (2100, 12)
        # "graduating 2027 (potentially 2028)" etc. is handled by min/max over all tokens
        return (lo, hi), _clip(line)
    return None, None


# ---------------------------------------------------------------- standing, return to school

SCHOOL_AFTER = re.compile(
    r"return(ing)? to (school|university|college|campus|your studies|academic)|"
    r"(semester|quarter|term|year)s? (of (school|study|studies|university) )?(remaining|left) "
    r"(after|following|post|upon)|continu\w+ (your|their) (studies|education|degree) (after|following)|"
    r"(remaining|left) (in|of) (your )?(university|school|academic) (studies )?(after|following|post)",
    re.I)
STANDING_WORDS = {"freshman": 1, "freshmen": 1, "first-year": 1, "first year": 1,
                  "sophomore": 2, "sophomores": 2, "second-year": 2, "second year": 2,
                  "junior": 3, "juniors": 3, "third-year": 3, "third year": 3, "penultimate": 3,
                  "senior": 4, "seniors": 4, "fourth-year": 4, "fourth year": 4, "final-year": 4,
                  "final year": 4}
STANDING_NAMES = {1: "freshman", 2: "sophomore", 3: "junior", 4: "senior"}
STUDENT_CTX = re.compile(r"\b(students?|undergrad\w*|university|college|currently|rising|degree|studies|class)\b", re.I)
NOT_STUDENT = re.compile(r"\bsenior (engineer|staff|leader|manager|management|executive|stakeholder|"
                         r"design|software|team|technical|director|vice|level|people)|senior design|capstone", re.I)


def extract_standing(lines: list[str]):
    for line in lines:
        low = line.lower()
        if NOT_STUDENT.search(low) or not STUDENT_CTX.search(low):
            continue
        # "completed (at least) the second year" -> current standing >= that year
        m = re.search(r"complet\w+ (at least )?(the |your |their )?(first|second|third|1st|2nd|3rd)[- ]year", low)
        if m:
            n = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}[m.group(3)]
            # finishing year n by the internship means currently in year n or later
            return [STANDING_NAMES[k] for k in range(n, 5)], False, _clip(line)
        m = re.search(r"\brising (freshm[ae]n|sophomores?|juniors?|seniors?)\b", low)
        if m:
            n = STANDING_WORDS[m.group(1)] - 1
            return ([STANDING_NAMES[n]] if n >= 1 else []), bool(PREFERRED.search(low)), _clip(line)
        framed = re.findall(
            r"\b(?:currently (?:an? )?|current |must be (?:an? )?|open to |for |incoming )"
            r"((?:freshm[ae]n|sophomores?|juniors?|seniors?|first[- ]year|second[- ]year|third[- ]year|"
            r"penultimate|final[- ]year)(?:(?:\s*(?:,|or|and|/)\s*)(?:freshm[ae]n|sophomores?|juniors?|seniors?))*)"
            r"(?=[\s,]*(?:students?|undergrad\w*|in college|in (?:a|an|your)|pursuing|year|standing|\b))", low)
        found = sorted({STANDING_WORDS[w.replace("-", " ")] if w.replace("-", " ") in STANDING_WORDS else STANDING_WORDS[w]
                        for grp in framed for w in re.findall(
                            r"freshm[ae]n|sophomores?|juniors?|seniors?|first[- ]year|second[- ]year|third[- ]year|"
                            r"penultimate|final[- ]year", grp)})
        found_student = re.findall(r"\b(freshm[ae]n|sophomores?|juniors?|seniors?)\s+(?:students?|undergrad\w*)", low)
        found = sorted(set(found) | {STANDING_WORDS[w] for w in found_student})
        if found and not re.search(r"\bph\.?d", low):
            return [STANDING_NAMES[n] for n in found], bool(PREFERRED.search(low)), _clip(line)
    return [], False, None


def current_standing(grad: tuple[int, int], now: datetime) -> int:
    """1-4 (freshman..senior) from expected graduation, at the time you're applying."""
    months_left = (grad[0] - now.year) * 12 + (grad[1] - now.month)
    years_left = months_left / 12
    if years_left <= 1.0:
        return 4
    if years_left <= 2.0:
        return 3
    if years_left <= 3.0:
        return 2
    return 1


# ---------------------------------------------------------------- degree, major

DEG_PATTERNS = {
    "bs": re.compile(r"\b(b\.?s\.?|b\.?a\.?|bachelor'?s?|undergrad(uate)?|bsc|b\.?eng)\b", re.I),
    "ms": re.compile(r"\b(m\.?s\.?|master'?s?|msc|m\.?eng)\b", re.I),
    "phd": re.compile(r"\b(ph\.?\s?d\.?|doctora(l|te))\b", re.I),
    "mba": re.compile(r"\bmba\b", re.I),
}
ENROLL = re.compile(r"\b(enrolled in|pursuing|working towards?|candidate for|currently (in|completing)|"
                    r"student (in|pursuing)|studying (for|towards))\b", re.I)


def extract_degrees(lines: list[str]):
    for line in lines:
        m = ENROLL.search(line)
        if not m:
            continue
        seg = line[m.start(): m.end() + 90]
        found = {d for d, p in DEG_PATTERNS.items() if p.search(seg)}
        if re.search(r"\bgraduate (program|degree|student|school)", seg, re.I) and not re.search(r"undergraduate", seg, re.I):
            found |= {"ms", "phd"}
        if found:
            return sorted(found, key=_deg_order), _clip(line)
    return [], None


FIELD_FAMILIES = {
    "cs": r"computer science|\bcs\b|software engineering|computer engineering|\beecs\b|informatics|computing|"
          r"computer and information|information science",
    "ee": r"electrical|electronics?|ece\b|communication engineering",
    "me": r"mechanical|mechatronics",
    "aero": r"aerospace|aeronautic|astronautic",
    "math": r"math\w*|statistics|applied math",
    "physics": r"physics",
    "data": r"data science|machine learning|artificial intelligence",
    "ops": r"industrial engineering|operations research|supply chain|operation management",
    "business": r"business|finance|accounting|economics|marketing|management|communications|public policy|"
                r"political science|human resources",
    "design": r"design|illustration|hci|human[- ]computer",
    "science": r"chemistry|biology|materials science|chemical engineering|bioengineering",
}
GENERIC_TECH = re.compile(r"\b(stem|technical (field|discipline|degree)|quantitative (field|discipline)|"
                          r"engineering (discipline|field)|related technical|engineering or related)\b", re.I)
TECH_FAMILIES = {"cs", "ee", "me", "aero", "math", "physics", "data", "ops", "science"}
FIELD_CUE = re.compile(r"\b(degree|major(ing)?|studying|field of study|pursuing|enrolled)\b[^\n]{0,40}?\bin\b", re.I)


def classify_field(text: str) -> set[str]:
    return {fam for fam, pat in FIELD_FAMILIES.items() if re.search(pat, text, re.I)}


def extract_fields(lines: list[str]):
    for line in lines:
        m = FIELD_CUE.search(line)
        if not m:
            continue
        seg = line[m.end(): m.end() + 200]
        fams = classify_field(seg)
        generic = bool(GENERIC_TECH.search(seg))
        if fams or generic:
            if generic:
                fams.add("tech")
            related = bool(re.search(r"related|similar|equivalent|other", seg, re.I))
            return sorted(fams), related, _clip(line)
    return [], False, None


# ---------------------------------------------------------------- GPA

GPA = re.compile(
    r"(?:gpa|grade point average)\D{0,40}?([2-3]\.\d{1,2})|([2-3]\.\d{1,2})\s*(?:/\s*4(?:\.0)?\s*)?"
    r"(?:\+|or (?:higher|above|better|greater))?\s*(?:cumulative\s+|overall\s+)?(?:gpa|grade point)", re.I)


def extract_gpa(lines: list[str]):
    for line in lines:
        m = GPA.search(line)
        if m:
            return float(m.group(1) or m.group(2)), bool(PREFERRED.search(line)), _clip(line)
    return None, False, None


# ---------------------------------------------------------------- authorization, clearance

US = r"u\.?\s?s\.?|united states|american"
REQ = re.compile(r"\b(must|required|requires?|requirement|only|need to|needs to|eligib\w+ for (this|the) (role|position))\b", re.I)
US_PERSON = re.compile(rf"({US}) person|permanent resident|green card|lawful permanent|refugee|asylee|\bitar\b|"
                       r"export (control|regulation|administration)", re.I)
CITIZEN = re.compile(rf"({US}) citizen(ship)?|citizenship", re.I)
NO_SPONSOR = re.compile(
    r"(unable to|will not|won't|cannot|can ?not|does not|do not|not able to|not (be )?(able|eligible) to|no longer)"
    r" (provide |offer |support |be able to )?(any )?(visa |work visa |immigration |employment visa )?sponsor|"
    r"without (the )?(need for |requiring )?(current or future )?(visa |employer |immigration |employment )?sponsorship|"
    r"\bno (visa |immigration )?sponsorship|not (offer|provide)\w* (visa|immigration) (sponsorship|support)|"
    r"sponsorship (is )?not (available|provided|offered)", re.I)
CLEAR_ACTIVE = re.compile(r"\b(active|current|existing)\b[^\n]{0,30}\b(clearance|ts/sci|top secret)", re.I)
CLEAR_ELIGIBLE = re.compile(r"(eligib\w+|ability|able|willing\w*)\b[^\n]{0,30}\b(obtain|hold|maintain|receive)\b"
                            r"[^\n]{0,30}\b(clearance)|clearance eligib\w+|(obtain|get) (a |an )?(\w+ )?clearance", re.I)


def extract_authorization(lines: list[str]):
    auth, text = None, None
    rank = {"no_sponsorship": 1, "us_person": 2, "us_citizen": 3}
    for line in lines:
        found = None
        if NO_SPONSOR.search(line) and not re.search(r"export licen[cs]e", line, re.I):
            found = "no_sponsorship"
        if REQ.search(line):
            if US_PERSON.search(line) and (re.search(r"\bitar\b|export", line, re.I) is None or REQ.search(line)):
                found = "us_person"
            elif CITIZEN.search(line) and re.search(rf"({US}) citizen", line, re.I):
                found = "us_citizen"
            if re.search(r"export licen[cs]e|may (be|require)", line, re.I) and found == "us_person":
                found = None  # "may require an export license": not a hard requirement
        if found and (auth is None or rank[found] > rank[auth]):
            auth, text = found, _clip(line)
    return auth, text


def extract_clearance(lines: list[str]):
    best, pref, text = None, False, None
    for line in lines:
        if "clearance" not in line.lower() and "ts/sci" not in line.lower():
            continue
        level = None
        if CLEAR_ACTIVE.search(line) and not re.search(r"\bor\b[^\n]{0,40}(eligib|obtain)", line, re.I):
            level = "active"
        elif CLEAR_ELIGIBLE.search(line) or CLEAR_ACTIVE.search(line):
            level = "eligible"
        if level and (best is None or level == "active"):
            best, text = level, _clip(line)
            pref = bool(PREFERRED.search(line) or re.search(r"may be required|depending on", line, re.I))
    return best, pref, text


# ---------------------------------------------------------------- school country

COUNTRY_CODES = {
    "canada": "CA", "canadian": "CA", "united states": "US", "u.s.": "US", "us": "US", "usa": "US",
    "united kingdom": "GB", "uk": "GB", "australia": "AU", "india": "IN", "singapore": "SG",
    "germany": "DE", "netherlands": "NL", "france": "FR", "ireland": "IE", "poland": "PL",
    "japan": "JP", "china": "CN", "taiwan": "TW", "israel": "IL", "switzerland": "CH",
    "hong kong": "HK", "korea": "KR", "south korea": "KR", "brazil": "BR", "mexico": "MX",
}
SCHOOL_COUNTRY = re.compile(
    r"(university|college|school|institution|program)s? (located |based )?in (the )?"
    r"(?P<c>canada|united states|u\.s\.|usa|us|united kingdom|uk|australia|india|singapore|germany|"
    r"netherlands|france|ireland|poland|japan|china|taiwan|israel|switzerland|hong kong|south korea|korea|"
    r"brazil|mexico)\b|(?P<c2>canadian|u\.s\.|us|american) (university|college|institution)", re.I)


def extract_school_country(lines: list[str]):
    for line in lines:
        m = SCHOOL_COUNTRY.search(line)
        if m and (REQ.search(line) or re.search(r"\(required\)|enrolled|attending|currently", line, re.I)):
            name = (m.group("c") or m.group("c2")).lower()
            name = "us" if name == "american" else name
            negated = bool(re.search(r"\b(cannot|can't|can not|must not|may not|not be|not currently|non-)\b"
                                     r"[^\n]{0,40}" + re.escape(m.group(0)[:12]), line, re.I))
            return COUNTRY_CODES.get(name), negated, _clip(line)
    return None, False, None


# ---------------------------------------------------------------- term, duration, pay

TERM = re.compile(r"\b(summer|fall|autumn|winter|spring)\s*(?:of\s*|')?(20\d\d|\d\d)\b|"
                  r"\b(20\d\d)\s+(summer|fall|autumn|winter|spring)\b", re.I)
YEAR_ONLY = re.compile(r"[(\s](20[2-3]\d)[)\s]|^(20[2-3]\d)\b|\b(20[2-3]\d)$")
START_MONTH = re.compile(
    r"\b(start\w*|begin\w*|from)\b[^\n]{0,30}?\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b(?:\s*(?:or|and|-|–|/)\s*\w+)?\s*,?\s*(20\d\d)", re.I)
TERM_CTX = re.compile(r"intern|co-?op|program|start|begin|weeks?|cohort|session", re.I)
MONTH_SEASON = {1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "summer", 6: "summer", 7: "summer",
                8: "fall", 9: "fall", 10: "fall", 11: "fall", 12: "winter"}


def _season(s: str) -> str:
    s = s.lower()
    return "fall" if s == "autumn" else s


def extract_terms(title: str, lines: list[str]) -> list[tuple[str | None, int]]:
    def from_text(text: str) -> list[tuple[str | None, int]]:
        out = []
        for m in TERM.finditer(text):
            if m.group(1):
                y = int(m.group(2)) if len(m.group(2)) == 4 else 2000 + int(m.group(2))
                out.append((_season(m.group(1)), y))
            else:
                out.append((_season(m.group(4)), int(m.group(3))))
        return out

    terms = from_text(title)
    if not terms:
        for line in lines:
            if GRAD_CUE.search(line) or not TERM_CTX.search(line):
                continue
            terms = from_text(line)
            m = START_MONTH.search(line)
            if not terms and m:
                terms = [(MONTH_SEASON[MONTHS[m.group(2).lower()[:3]]], int(m.group(3)))]
            if terms:
                break
    if not terms:
        y = YEAR_ONLY.search(title)
        if y:
            terms = [(None, int(next(g for g in y.groups() if g)))]
    return sorted(set(terms), key=lambda t: (t[1], t[0] or ""))


NUM_WORDS = {"eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
             "fourteen": 14, "fifteen": 15, "sixteen": 16, "six": 6, "four": 4, "three": 3}
WEEKS = re.compile(r"\b(\d{1,2}|" + "|".join(NUM_WORDS) + r")(?:\s*(?:-|–|to)\s*(\d{1,2}))?[- ](week|month)s?\b", re.I)


def extract_weeks(lines: list[str]):
    for line in lines:
        if not re.search(r"intern|co-?op|program|placement|position|role", line, re.I):
            continue
        m = WEEKS.search(line)
        if m:
            lo = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1).lower()]
            hi = int(m.group(2)) if m.group(2) else lo
            if m.group(3).lower() == "month":
                lo, hi = round(lo * 4.33), round(hi * 4.33)
            if 4 <= lo <= 60 and hi >= lo:
                return lo, hi
    return None


MONEY = re.compile(r"(?P<cur>US\$|CA\$|C\$|CAD|USD|£|€|\$)\s?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<k>[kK])?")
UNIT = re.compile(r"\b(per hour|an hour|/\s?h(ou)?r|hourly|per month|a month|/\s?mo(nth)?|monthly|"
                  r"per week|a week|/\s?w(ee)?k|weekly|per year|a year|/\s?y(ea)?r|annual\w*|salary)\b|/hr\b|/mo\b",
                  re.I)
HOURS_PER = {"hour": 1, "week": 40, "month": 173.3, "year": 2080}


def _unit(text: str) -> str | None:
    """Pay interval. A specific unit ("per week") beats a generic word ("salary")."""
    found = [m.group(0).lower() for m in UNIT.finditer(text)]
    for u in found:
        if re.search(r"hour|hr|/\s?h", u):
            return "hour"
        if "mo" in u:
            return "month"
        if re.search(r"week|wk", u):
            return "week"
    return "year" if found else None


def extract_pay(lines: list[str]):
    for line in lines:
        if not re.search(r"\$|USD|CAD|£|€", line) or re.search(r"401\(?k|stipend|reimburse|bonus", line, re.I):
            continue
        amounts = list(MONEY.finditer(line))
        if not amounts:
            continue
        unit = _unit(line)
        if not unit:
            continue
        cur = amounts[0].group("cur").upper()
        if cur not in ("$", "US$", "USD") or re.search(r"\bCAD\b|C\$|CA\$", line):
            continue  # only USD is normalized; other currencies don't compare to min_hourly_pay
        vals = []
        for a in amounts:
            v = float(a.group("num").replace(",", "")) * (1000 if a.group("k") else 1)
            vals.append(v / HOURS_PER[unit])
        vals = [v for v in vals if 8 <= v <= 400]  # sane hourly band; drops typos like $12,5000
        if vals:
            return (round(min(vals), 2), round(max(vals), 2)), _clip(line)
    return None, None


# ---------------------------------------------------------------- extract all


def extract(p: Posting) -> Requirements:
    lines = _lines(p.description)
    r = Requirements()
    r.grad_window, r.grad_text = extract_grad_window(lines)
    for line in lines:
        if SCHOOL_AFTER.search(line):
            r.school_after, r.school_after_text = True, _clip(line)
            break
    r.standings, r.standings_preferred, r.standing_text = extract_standing(lines)
    r.degrees, r.degree_text = extract_degrees(lines)
    r.fields, r.fields_related_ok, r.fields_text = extract_fields(lines)
    r.gpa_min, r.gpa_preferred, r.gpa_text = extract_gpa(lines)
    r.authorization, r.authorization_text = extract_authorization(lines)
    r.clearance, r.clearance_preferred, r.clearance_text = extract_clearance(lines)
    country, negated, r.school_country_text = extract_school_country(lines)
    if negated:
        r.school_country_not = country
    else:
        r.school_country = country
    r.terms = extract_terms(p.title, lines)
    r.weeks = extract_weeks(lines)
    r.pay_hourly, r.pay_text = extract_pay(lines)
    r.housing = bool(re.search(r"housing (stipend|support|provided|allowance)|corporate housing|relocation stipend",
                               p.description or "", re.I))
    return r


# ---------------------------------------------------------------- checks


@dataclass
class Check:
    status: str  # ok | fail | warn
    text: str


TERM_END_MONTH = {"spring": 5, "summer": 8, "fall": 12, "winter": 3}


def check(req: Requirements, prof: Profile, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(UTC)
    out: list[Check] = []
    grad = prof.grad_ym

    if req.grad_window and grad:
        lo, hi = req.grad_window
        # one month of slack each side: "June 2028" vs a May 2028 graduation
        lo_s = (lo[0], lo[1] - 1) if lo[1] > 1 else (lo[0] - 1, 12)
        hi_s = (hi[0], hi[1] + 1) if hi[1] < 12 else (hi[0] + 1, 1)
        ok = lo_s <= grad <= hi_s
        out.append(Check("ok" if ok else "fail",
                         f"graduation {_fmt_window(req.grad_window)} (you: {_fmt_ym(grad)})"
                         + ("" if ok else f": \"{req.grad_text}\"")))

    if req.school_after and grad:
        # internship term: the posting's, else your earliest target term
        terms = [t for t in req.terms if t[0]] or [_parse_term_key(t) for t in prof.terms]
        if terms:
            season, year = min(terms, key=lambda t: (t[1], TERM_END_MONTH[t[0]]))
            end = (year + (1 if season == "winter" else 0), TERM_END_MONTH[season])
            ok = grad > end
            out.append(Check("ok" if ok else "fail",
                             f"must return to school after the internship (you graduate {_fmt_ym(grad)})"
                             + ("" if ok else f": \"{req.school_after_text}\"")))

    if req.standings and grad:
        mine = STANDING_NAMES[current_standing(grad, now)]
        ok = mine in req.standings
        status = "ok" if ok else ("warn" if req.standings_preferred else "fail")
        out.append(Check(status, f"year in school: {'/'.join(req.standings)}"
                         + (" preferred" if req.standings_preferred else "") + f" (you: {mine})"
                         + ("" if ok else f": \"{req.standing_text}\"")))

    if req.degrees and prof.degree_level:
        ok = prof.degree_level in req.degrees
        out.append(Check("ok" if ok else "fail",
                         f"degree {'/'.join(d.upper() if d != 'phd' else 'PhD' for d in req.degrees)}"
                         f" (you: {prof.degree_level.upper() if prof.degree_level != 'phd' else 'PhD'})"
                         + ("" if ok else f": \"{req.degree_text}\"")))

    if req.fields and prof.majors:
        mine = set().union(*(classify_field(m) for m in prof.majors)) or {"other"}
        listed = set(req.fields) - {"tech"}
        if mine & listed:
            out.append(Check("ok", f"major listed ({', '.join(sorted(mine & listed))})"))
        elif "tech" in req.fields and mine & TECH_FAMILIES:
            out.append(Check("ok", "major: technical/quantitative field accepted"))
        else:
            strict = prof.strict_majors or not req.fields_related_ok
            out.append(Check("fail" if strict else "warn",
                             f"major not listed ({', '.join(sorted(listed)) or 'specific fields'}"
                             + (", 'or related field'" if req.fields_related_ok else "") + f"): \"{req.fields_text}\""))

    if req.gpa_min is not None and prof.gpa is not None:
        ok = prof.gpa >= req.gpa_min
        status = "ok" if ok else ("warn" if req.gpa_preferred else "fail")
        out.append(Check(status, f"GPA ≥ {req.gpa_min:g}{' preferred' if req.gpa_preferred else ''} (you: {prof.gpa:g})"))

    if req.authorization and (prof.citizenship or prof.needs_sponsorship is not None):
        cit, sponsor = prof.citizenship, prof.needs_sponsorship
        if req.authorization == "us_citizen":
            ok = cit == "us_citizen" if cit else None
        elif req.authorization == "us_person":
            ok = cit in ("us_citizen", "permanent_resident") if cit else None
        else:  # no_sponsorship
            ok = (not sponsor) if sponsor is not None else (cit in ("us_citizen", "permanent_resident") if cit else None)
        if ok is not None:
            label = {"us_citizen": "US citizens only", "us_person": "US persons only (citizen/PR)",
                     "no_sponsorship": "no visa sponsorship"}[req.authorization]
            out.append(Check("ok" if ok else "fail", f"{label}" + ("" if ok else f": \"{req.authorization_text}\"")))

    if req.clearance and prof.clearance:
        if req.clearance == "active":
            ok = prof.clearance == "active"
        else:
            ok = prof.clearance in ("eligible", "active")
        if not ok and prof.clearance == "none" and prof.citizenship == "us_citizen" and req.clearance == "eligible":
            ok = True  # you said none, but a US citizen is normally eligible to obtain one
        status = "ok" if ok else ("warn" if req.clearance_preferred else "fail")
        out.append(Check(status, f"security clearance {'(active) ' if req.clearance == 'active' else 'eligibility '}"
                         + ("preferred" if req.clearance_preferred else "required")
                         + ("" if ok else f": \"{req.clearance_text}\"")))

    if req.school_country and prof.school_country:
        ok = req.school_country == prof.school_country.upper()
        out.append(Check("ok" if ok else "fail", f"school must be in {req.school_country}"
                         + ("" if ok else f": \"{req.school_country_text}\"")))

    if req.school_country_not and prof.school_country:
        ok = req.school_country_not != prof.school_country.upper()
        out.append(Check("ok" if ok else "fail", f"school must not be in {req.school_country_not}"
                         + ("" if ok else f": \"{req.school_country_text}\"")))

    if req.terms and prof.terms:
        wanted = [_parse_term_key(t) for t in prof.terms]
        ok = any((s is None or s == ws) and y == wy for s, y in req.terms for ws, wy in wanted)
        label = ", ".join(f"{(s or '').title()} {y}".strip() for s, y in req.terms)
        out.append(Check("ok" if ok else "fail", f"term {label}"
                         + ("" if ok else f" (you want {', '.join(t.replace('-', ' ').title() for t in prof.terms)})")))

    if req.pay_hourly and prof.min_hourly_pay:
        ok = req.pay_hourly[1] >= prof.min_hourly_pay
        out.append(Check("ok" if ok else "fail",
                         f"pay ${req.pay_hourly[1]:.0f}/hr (min ${prof.min_hourly_pay:g})"))
    return out
