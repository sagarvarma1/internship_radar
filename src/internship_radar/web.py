"""Public multi-user web app.

Anyone can browse open internships. Signing in (email link or 6-digit code, no passwords) adds a
personal "For you" list matched against your profile, saved/applied/hidden statuses, and email
alerts (instantly when a match is posted, plus a nightly digest).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, mailer
from .config import load_registry, load_settings
from .filters import FilterConfig, score
from .report import coverage_data
from .requirements import Profile
from .users import profile_complete, user_config

settings = load_settings()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app = FastAPI(title="Internship Radar", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)

SESSION_COOKIE = "radar_session"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
STATUSES = ("interested", "applied", "skipped")
ROLE_LABELS = {"swe": "Software", "ml": "AI / ML", "research": "Research",
               "applied_science": "Applied science", "data": "Data", "hardware": "Hardware"}
TIMEZONES = ["America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York",
             "America/Phoenix", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
             "Europe/London", "Asia/Kolkata"]


def conn():
    return db.connect(settings.db_path)


# ---------------------------------------------------------------- security helpers


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """CSRF guard: state-changing requests must come from this site. Session cookies are also
    SameSite=Lax. The one-click unsubscribe POST is exempt (mail clients send it)."""
    if request.method == "POST" and not request.url.path.startswith("/unsubscribe/"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.url.netloc:
            return Response("Cross-site request blocked", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


def current_user(request: Request, c):
    return db.user_for_session(c, request.cookies.get(SESSION_COOKIE))


def require_user(request: Request, c):
    user = current_user(request, c)
    if not user:
        raise HTTPException(401, "Sign in first")
    return user


# ---------------------------------------------------------------- data (cached per poll)

_cache: dict = {"key": None, "rows": []}
_cache_lock = threading.Lock()


def open_internships(c) -> list[dict]:
    """All open internships with parsed analysis. Re-read only when a poll changed something."""
    key = c.execute(
        "SELECT MAX(last_seen_at), COUNT(*), MAX(first_seen_at), "
        "(SELECT COUNT(*) FROM postings WHERE closed_at IS NOT NULL) FROM postings").fetchone()
    key = tuple(key)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["rows"]
        tags = {r["key"]: tuple(json.loads(r["tags"])) for r in c.execute("SELECT key, tags FROM companies")}
        rows = []
        for r in c.execute(
            """SELECT uid, company_key, source, source_id, company, title, team, department, locations,
                      countries, remote, employment_type, posted_at, apply_url, first_seen_at,
                      is_internship, intern_reason, role_types, requirements, chips
               FROM postings WHERE closed_at IS NULL AND is_internship=1"""):
            rows.append({
                "uid": r["uid"], "company": r["company"], "title": r["title"],
                "team": r["team"] or r["department"], "locations": json.loads(r["locations"]),
                "remote": r["remote"], "apply_url": r["apply_url"], "first_seen_at": r["first_seen_at"],
                "posted_at": r["posted_at"], "chips": json.loads(r["chips"]),
                "roles": json.loads(r["role_types"]), "tags": tags.get(r["company_key"], ()),
                "requirements": json.loads(r["requirements"]),
                "_posting": db.row_to_posting(r), "_analysis": db.row_to_analysis(r),
            })
        _cache.update(key=key, rows=rows)
        return rows


def group_rows(rows: list[dict]) -> list[dict]:
    """Same company + title in several cities (Palantir lists one posting per office) -> one row
    listing every location, with a separate apply link per location."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["company"], r["title"])
        g = groups.get(key)
        if g is None:
            groups[key] = {**r, "variants": [r]}
            continue
        g["variants"].append(r)
        g["score"] = max(g["score"], r["score"])
        g["passes"] = g["passes"] or r["passes"]
        g["fresh_at"] = max(g["fresh_at"], r["fresh_at"])
        g["is_new"] = g["is_new"] or r["is_new"]
        g["status"] = g["status"] or r["status"]
        g["locations"] = list(dict.fromkeys(g["locations"] + r["locations"]))
    for g in groups.values():
        g["uids"] = [v["uid"] for v in g["variants"]]
    return list(groups.values())


def personalize(rows: list[dict], cfg: FilterConfig, statuses: dict[str, str]) -> list[dict]:
    now = datetime.now(UTC)
    out = []
    for r in rows:
        m = score(r["_posting"], r["_analysis"], cfg, r["tags"], now)
        d = {k: v for k, v in r.items() if not k.startswith("_")}
        # Age shown = when the company published it, if that's earlier than when we first saw it
        # (we've watched it since before it was posted); otherwise when we caught it.
        fresh = r["first_seen_at"]
        if r["posted_at"] and r["posted_at"] < fresh:
            fresh = r["posted_at"]
        d.update(score=m.score, passes=m.passes, status=statuses.get(r["uid"]), fresh_at=fresh,
                 checks=[x for x in m.reasons if x.startswith(("✓", "⚠", "filtered:"))],
                 why=[x for x in m.reasons if not x.startswith(("✓", "⚠", "filtered:"))])
        out.append(d)
    return out


def _age(iso: str | None) -> str:
    if not iso:
        return ""
    s = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 86400 * 60:
        return f"{s // 86400}d"
    return f"{s // (86400 * 30)}mo"


templates.env.filters["age"] = _age
templates.env.filters["sum_ord"] = lambda s: sum(map(ord, s or ""))
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


def site_stats(c) -> dict:
    last = c.execute("SELECT finished_at FROM poll_runs WHERE finished_at IS NOT NULL "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    n_companies = c.execute("SELECT COUNT(*) FROM companies WHERE active=1 AND ats!='unsupported'").fetchone()[0]
    return {"last_poll": last["finished_at"] if last else None, "companies": n_companies}


def upcoming_terms(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    seasons = ["winter", "spring", "summer", "fall"]
    idx = {1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2, 7: 2, 8: 3, 9: 3, 10: 3, 11: 3, 12: 3}[now.month]
    year, out = now.year, []
    for _ in range(7):
        idx += 1
        if idx == 4:
            idx, year = 0, year + 1
        out.append(f"{seasons[idx]}-{year}")
    return out


# ---------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def home(request: Request, tab: str | None = None):
    c = conn()
    try:
        user = current_user(request, c)
        tab = tab or ("foryou" if user else "all")
        if tab not in ("foryou", "all", "saved", "applied", "hidden") or (tab != "all" and not user):
            tab = "all"
        statuses = {}
        if user:
            statuses = {r["uid"]: r["status"] for r in c.execute(
                "SELECT uid, status FROM user_postings WHERE user_id=?", (user["id"],))}
        rows = personalize(open_internships(c), user_config(user, settings), statuses)
        counts = {
            "all": len(rows),
            "foryou": sum(1 for r in rows if r["passes"] and r["status"] != "skipped"),
            "saved": sum(1 for r in rows if r["status"] == "interested"),
            "applied": sum(1 for r in rows if r["status"] == "applied"),
            "hidden": sum(1 for r in rows if r["status"] == "skipped"),
        }
        if tab == "foryou":
            rows = [r for r in rows if r["passes"] and r["status"] != "skipped"]
        elif tab == "all":
            rows = [r for r in rows if r["status"] != "skipped"]
        else:
            want = {"saved": "interested", "applied": "applied", "hidden": "skipped"}[tab]
            rows = [r for r in rows if r["status"] == want]

        since = None
        if user:  # "new since your last visit": a visit ends after 30 idle minutes
            prev, cur, t = user["visit_prev"], user["visit_cur"], db.now()
            if not cur or datetime.now(UTC) - datetime.fromisoformat(cur) > timedelta(minutes=30):
                prev = cur
            c.execute("UPDATE users SET visit_prev=?, visit_cur=? WHERE id=?", (prev, t, user["id"]))
            since = prev
        for r in rows:
            r["is_new"] = bool(since and r["first_seen_at"] > since)
        rows = group_rows(rows)
        rows.sort(key=lambda r: r["fresh_at"], reverse=True)
        terms = sorted({f"{s}-{y}" for r in rows for s, y in r["requirements"].get("terms", []) if s},
                       key=lambda t: (t.split("-")[1], ["winter", "spring", "summer", "fall"].index(t.split("-")[0])))
        return templates.TemplateResponse(request, "home.html", {
            "user": user, "tab": tab, "rows": rows, "counts": counts, "terms": terms,
            "stats": site_stats(c), "needs_profile": bool(user and not profile_complete(user)),
            "new_count": sum(r["is_new"] for r in rows),
        })
    finally:
        c.close()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, email: str = ""):
    return templates.TemplateResponse(request, "login.html", {"user": None, "email": email, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return templates.TemplateResponse(request, "login.html",
                                          {"user": None, "email": email, "error": "That doesn't look like an email address."},
                                          status_code=400)
    c = conn()
    try:
        ip = client_ip(request)
        limited = (db.recent_login_requests(c, email=email) >= 5
                   or db.recent_login_requests(c, ip=ip) >= 20
                   or db.recent_login_requests(c) >= 500)
        if not limited:
            token, code = db.create_login_token(c, email, ip)
            link = f"{settings.site_url}/auth/{token}"
            try:
                mailer.send(mailer.Message(
                    to=email,
                    subject=f"Your Internship Radar sign-in code: {code}",
                    text=(f"Sign in to Internship Radar:\n{link}\n\nOr enter this code: {code}\n\n"
                          "The link and code expire in 15 minutes. If you didn't ask for this, ignore this email."),
                    html=templates.get_template("email_login.html").render(link=link, code=code),
                ))
            except Exception as e:
                print(f"login email failed: {type(e).__name__}: {e}", flush=True)
                return templates.TemplateResponse(request, "login.html", {
                    "user": None, "email": email,
                    "error": "We couldn't send the email right now. Please try again in a minute."},
                    status_code=503)
        # Same response whether or not we sent (rate limit) so this can't be used to probe.
        return templates.TemplateResponse(request, "check_email.html", {"user": None, "email": email, "error": None})
    finally:
        c.close()


def _start_session(c, email: str, request: Request) -> Response:
    user, created = db.get_or_create_user(c, email, settings.filters.to_dict())
    token = db.create_session(c, user["id"])
    dest = "/settings?welcome=1" if created or not profile_complete(user) else "/"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=int(db.SESSION_TTL.total_seconds()), httponly=True,
                    samesite="lax", secure=settings.secure_cookies)
    return resp


@app.get("/auth/{token}")
def auth_link(token: str, request: Request):
    c = conn()
    try:
        email = db.consume_login_token(c, token)
        if not email:
            return templates.TemplateResponse(request, "login.html", {
                "user": None, "email": "", "error": "That sign-in link has expired or was already used. "
                                                     "Enter your email for a new one."}, status_code=400)
        return _start_session(c, email, request)
    finally:
        c.close()


@app.post("/auth/code")
def auth_code(request: Request, email: str = Form(...), code: str = Form(...)):
    email = email.strip().lower()
    c = conn()
    try:
        if re.fullmatch(r"\s*\d{6}\s*", code) and db.consume_login_code(c, email, code):
            return _start_session(c, email, request)
        return templates.TemplateResponse(request, "check_email.html", {
            "user": None, "email": email, "error": "That code didn't work. Check the latest email, "
                                                   "or request a new one."}, status_code=400)
    finally:
        c.close()


@app.post("/logout")
def logout(request: Request):
    c = conn()
    try:
        db.delete_session(c, request.cookies.get(SESSION_COOKIE))
    finally:
        c.close()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- settings


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, welcome: int = 0, saved: int = 0):
    c = conn()
    try:
        user = current_user(request, c)
        if not user:
            return RedirectResponse("/login", status_code=303)
        cfg = user_config(user, settings)
        tags = sorted({t for co in load_registry(settings.registry_path) for t in co.tags})
        return templates.TemplateResponse(request, "settings.html", {
            "user": user, "p": cfg.profile, "f": cfg, "welcome": welcome, "saved": saved,
            "terms": upcoming_terms(), "roles": ROLE_LABELS, "tags": tags, "timezones": TIMEZONES,
            "errors": [],
        })
    finally:
        c.close()


def _num(v: str | None, cast=float):
    v = (v or "").strip()
    return cast(v) if v else None


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    c = conn()
    try:
        user = current_user(request, c)
        if not user:
            return RedirectResponse("/login", status_code=303)
        errors = []
        try:
            sponsor = form.get("needs_sponsorship")
            profile = Profile.from_dict({
                "graduation": (form.get("graduation") or None),
                "degree_level": form.get("degree_level") or None,
                "majors": [m.strip() for m in (form.get("majors") or "").split(",") if m.strip()][:5],
                "gpa": _num(form.get("gpa")),
                "citizenship": form.get("citizenship") or None,
                "needs_sponsorship": {"yes": True, "no": False}.get(sponsor),
                "clearance": form.get("clearance") or None,
                "school_country": form.get("school_country") or None,
                "terms": [t for t in form.getlist("terms") if t in upcoming_terms()],
                "min_hourly_pay": _num(form.get("min_hourly_pay")),
                "strict_majors": form.get("strict_majors") == "on",
            })
            if profile.gpa is not None and not 0 <= profile.gpa <= 5:
                raise ValueError("GPA should be between 0 and 5")
            filters = FilterConfig.from_dict({
                "role_types": [r for r in form.getlist("role_types") if r in ROLE_LABELS],
                "countries": ["US"] if form.get("location_scope") != "anywhere" else [],
                "allow_remote": form.get("allow_remote") == "on",
                "allow_unknown_location": True,
                "preferred_cities": [x.strip() for x in (form.get("preferred_cities") or "").split(",") if x.strip()][:10],
                "preferred_tags": [t for t in form.getlist("preferred_tags")][:10],
                "min_score": 0,
            }).to_dict()
            digest_hour = int(form.get("digest_hour") or 20)
            tz = form.get("timezone") if form.get("timezone") in TIMEZONES else "America/Los_Angeles"
            if not 0 <= digest_hour <= 23:
                raise ValueError("Pick a digest time")
        except (ValueError, TypeError) as e:
            errors.append(str(e))
        if errors:
            cfg = user_config(user, settings)
            tags = sorted({t for co in load_registry(settings.registry_path) for t in co.tags})
            return templates.TemplateResponse(request, "settings.html", {
                "user": user, "p": cfg.profile, "f": cfg, "welcome": 0, "saved": 0,
                "terms": upcoming_terms(), "roles": ROLE_LABELS, "tags": tags,
                "timezones": TIMEZONES, "errors": errors}, status_code=400)
        c.execute(
            """UPDATE users SET profile=?, filters=?, instant_alerts=?, nightly_digest=?,
                 digest_hour=?, timezone=? WHERE id=?""",
            (json.dumps(profile.to_dict()), json.dumps(filters), int(form.get("instant_alerts") == "on"),
             int(form.get("nightly_digest") == "on"), digest_hour, tz, user["id"]),
        )
        return RedirectResponse("/?tab=foryou" if form.get("welcome") else "/settings?saved=1", status_code=303)
    finally:
        c.close()


@app.post("/account/delete")
def delete_account(request: Request):
    c = conn()
    try:
        user = require_user(request, c)
        db.delete_user(c, user["id"])
    finally:
        c.close()
    resp = RedirectResponse("/?deleted=1", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- unsubscribe (no sign-in needed)


def _user_by_unsub(c, token: str):
    return c.execute("SELECT * FROM users WHERE unsubscribe_token=?", (token,)).fetchone()


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, request: Request):
    c = conn()
    try:
        u = _user_by_unsub(c, token)
        return templates.TemplateResponse(request, "unsubscribe.html",
                                          {"user": None, "found": bool(u), "done": False, "token": token})
    finally:
        c.close()


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, request: Request):
    c = conn()
    try:
        u = _user_by_unsub(c, token)
        if u:
            c.execute("UPDATE users SET instant_alerts=0, nightly_digest=0 WHERE id=?", (u["id"],))
        return templates.TemplateResponse(request, "unsubscribe.html",
                                          {"user": None, "found": bool(u), "done": True, "token": token})
    finally:
        c.close()


# ---------------------------------------------------------------- API


@app.post("/api/postings/{uid:path}/status")
async def set_status(uid: str, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in (*STATUSES, "new"):
        raise HTTPException(400, "bad status")
    c = conn()
    try:
        user = require_user(request, c)
        if not c.execute("SELECT 1 FROM postings WHERE uid=?", (uid,)).fetchone():
            raise HTTPException(404, "no such posting")
        db.set_status(c, user["id"], uid, status)
    finally:
        c.close()
    return {"uid": uid, "status": status}


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    data = coverage_data(settings)
    c = conn()
    try:
        user = current_user(request, c)
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM companies WHERE active=1 AND ats!='unsupported'
               ORDER BY open_internships DESC, name COLLATE NOCASE""")]
        stats = site_stats(c)
    finally:
        c.close()
    for r in rows:
        r["tags"] = json.loads(r["tags"])
    return templates.TemplateResponse(request, "companies.html", {
        "user": user, "rows": rows, "data": data, "stats": stats,
        "per_ats": dict(sorted(data["per_ats"].items()))})


@app.get("/healthz")
def healthz():
    c = conn()
    try:
        last = c.execute("SELECT finished_at FROM poll_runs WHERE finished_at IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        c.close()
    stale = True
    if last:
        stale = datetime.now(UTC) - datetime.fromisoformat(last["finished_at"]) > timedelta(minutes=5)
    return JSONResponse({"ok": True, "last_poll": last["finished_at"] if last else None,
                         "poll_stale": stale, "users": users, "mail": mailer.backend()})


