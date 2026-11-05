"""Web app: open internships list, per-posting status, company health."""

from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .config import load_registry, load_settings
from .models import ATS
from .report import coverage_data

settings = load_settings()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app = FastAPI(title="Internship Radar", docs_url=None, redoc_url=None)

STATUSES = ("new", "interested", "applied", "skipped")
SORTS = {"score": "p.score DESC, p.first_seen_at DESC", "new": "p.first_seen_at DESC, p.score DESC",
         "company": "p.company COLLATE NOCASE, p.score DESC"}
VISIT_GAP = timedelta(minutes=30)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """If RADAR_PASSWORD is set, everything except /healthz needs it (any username)."""
    password = os.environ.get("RADAR_PASSWORD")
    if password and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, password)
            except ValueError:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="radar"'})
    return await call_next(request)


def conn():
    return db.connect(settings.db_path)


def tag_options() -> list[str]:
    return sorted({t for c in load_registry(settings.registry_path) for t in c.tags})


def query_postings(c, params: dict, since: str | None) -> list[dict]:
    where = ["p.closed_at IS NULL", "p.is_internship=1"]
    args: list = []
    if params.get("all") != "1":
        where.append("p.passes=1")
    if params.get("tag"):
        where.append("EXISTS (SELECT 1 FROM json_each(c.tags) WHERE value=?)")
        args.append(params["tag"])
    if params.get("role"):
        where.append("EXISTS (SELECT 1 FROM json_each(p.role_types) WHERE value=?)")
        args.append(params["role"])
    if params.get("remote"):
        where.append("p.remote=?")
        args.append(params["remote"])
    if params.get("status"):
        where.append("p.status=?")
        args.append(params["status"])
    elif params.get("hide_skipped", "1") == "1":
        where.append("p.status!='skipped'")
    if params.get("loc"):
        where.append("p.locations LIKE ?")
        args.append(f"%{params['loc']}%")
    if params.get("q"):
        where.append("(p.title LIKE ? OR p.company LIKE ? OR p.team LIKE ? OR p.department LIKE ?)")
        args += [f"%{params['q']}%"] * 4
    if params.get("new") == "1" and since:
        where.append("p.first_seen_at>?")
        args.append(since)
    order = SORTS.get(params.get("sort", "score"), SORTS["score"])
    rows = c.execute(
        f"""SELECT p.uid, p.company, p.title, p.team, p.department, p.locations, p.remote,
                   p.employment_type, p.posted_at, p.first_seen_at, p.apply_url, p.score,
                   p.reasons, p.role_types, p.status, p.passes, p.source, c.tags
            FROM postings p JOIN companies c ON c.key=p.company_key
            WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 1000""",
        args,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["locations"] = json.loads(d["locations"])
        d["reasons"] = json.loads(d["reasons"])
        d["role_types"] = json.loads(d["role_types"])
        d["tags"] = json.loads(d["tags"])
        d["is_new"] = bool(since and d["first_seen_at"] > since)
        out.append(d)
    return out


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    delta = datetime.now(UTC) - datetime.fromisoformat(iso)
    s = int(delta.total_seconds())
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


templates.env.filters["ago"] = _ago


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    params = dict(request.query_params)
    now_iso = db.now()
    prev = request.cookies.get("visit_prev")
    cur = request.cookies.get("visit_cur")
    # Start a new "visit" after 30 idle minutes; "new" means first seen since the previous visit.
    if not cur or datetime.now(UTC) - datetime.fromisoformat(cur) > VISIT_GAP:
        prev, cur = cur, now_iso
    c = conn()
    try:
        rows = query_postings(c, params, prev)
        totals = c.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(passes),0) m FROM postings
               WHERE closed_at IS NULL AND is_internship=1"""
        ).fetchone()
        last_run = c.execute(
            "SELECT finished_at FROM poll_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        broken = c.execute(
            "SELECT COUNT(*) FROM companies WHERE active=1 AND consecutive_failures>=3"
        ).fetchone()[0]
    finally:
        c.close()
    resp = templates.TemplateResponse(
        request, "index.html",
        {
            "rows": rows, "params": params, "since": prev, "tags": tag_options(),
            "roles": ["swe", "ml", "research", "applied_science", "data", "hardware", "other"],
            "statuses": STATUSES, "total": totals["n"], "matching": totals["m"],
            "new_count": sum(r["is_new"] for r in rows),
            "last_poll": last_run["finished_at"] if last_run else None, "broken": broken,
        },
    )
    year = 60 * 60 * 24 * 365
    resp.set_cookie("visit_cur", now_iso, max_age=year, httponly=True, samesite="lax")
    if prev:
        resp.set_cookie("visit_prev", prev, max_age=year, httponly=True, samesite="lax")
    return resp


@app.post("/api/postings/{uid:path}/status")
async def set_status(uid: str, request: Request):
    body = await request.json()
    status = body.get("status")
    if status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    c = conn()
    try:
        if not c.execute("SELECT 1 FROM postings WHERE uid=?", (uid,)).fetchone():
            raise HTTPException(404, "no such posting")
        db.set_status(c, uid, status)
    finally:
        c.close()
    return {"uid": uid, "status": status}


@app.get("/api/postings")
def api_postings(request: Request):
    c = conn()
    try:
        return JSONResponse(query_postings(c, dict(request.query_params), None))
    finally:
        c.close()


@app.get("/health", response_class=HTMLResponse)
def health(request: Request):
    data = coverage_data(settings)
    c = conn()
    try:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM companies WHERE active=1 AND ats!='unsupported'
               ORDER BY consecutive_failures DESC, open_internships DESC, name COLLATE NOCASE"""
        )]
        runs = [dict(r) for r in c.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 12")]
    finally:
        c.close()
    for r in rows:
        r["tags"] = json.loads(r["tags"])
    return templates.TemplateResponse(
        request, "health.html",
        {"rows": rows, "runs": runs, "data": data, "unsupported": data["unsupported"],
         "per_ats": dict(sorted(data["per_ats"].items()))},
    )


@app.get("/healthz")
def healthz():
    """Liveness for the platform: also reports whether polling is keeping up."""
    c = conn()
    try:
        last = c.execute(
            "SELECT finished_at FROM poll_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    stale = True
    if last:
        age = datetime.now(UTC) - datetime.fromisoformat(last["finished_at"])
        stale = age > timedelta(minutes=settings.poll.interval_minutes * 4)
    return {"ok": True, "last_poll": last["finished_at"] if last else None, "poll_stale": stale}


__all__ = ["app", "ATS"]
