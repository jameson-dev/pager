"""FastAPI web interface + background log watcher."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import auth
from . import config as cfg
from . import events
from . import notify
from . import parser as parsermod
from . import pdfgen
from . import poppler
from . import printing
from . import processor
from . import pagermon_db
from .cleanup import CleanupWorker
from .database import JobStore
from .pagermon_watcher import PagerMonDbWatcher
from . import retry as retry_mod
from .processor import inject_test_page, reprint_job
from .retry import RetryWorker, configured_max_attempts
from .watcher import LogWatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("pager")

BASE = Path(__file__).parent
app = FastAPI(title="Pager")
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))

# Paths reachable without a session (so the login page can render + submit, and
# the health check stays usable for monitoring).
_AUTH_ALLOW_PREFIXES = ("/static/", "/login", "/api/login", "/api/auth/status",
                        "/favicon.ico", "/api/health")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Block all routes until the shared password is entered (when auth is on)."""
    path = request.url.path
    if auth.is_enabled() and not any(path == p or path.startswith(p) for p in _AUTH_ALLOW_PREFIXES):
        if not auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
            # Page requests get redirected to the login screen; API calls get 401.
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)

_store: JobStore | None = None
_watcher: LogWatcher | None = None
_db_watcher: PagerMonDbWatcher | None = None
_retry: RetryWorker | None = None
_cleanup: CleanupWorker | None = None
_watchdog: notify.WatchdogNotifier | None = None


def store() -> JobStore:
    assert _store is not None
    return _store


def _ingest_source(conf: dict) -> str:
    """Which ingest source is active: 'log' (default) or 'pagermon_db'."""
    src = (conf.get("ingest_source") or "log").strip().lower()
    return src if src in ("log", "pagermon_db") else "log"


@app.on_event("startup")
def _startup() -> None:
    global _store, _watcher, _db_watcher, _retry, _cleanup, _watchdog
    cfg.ensure_default_template()  # blank built-in template for a from-scratch build
    conf = cfg.load_config()
    _store = JobStore(conf.get("database", "data/jobs.db"))
    if _ingest_source(conf) == "pagermon_db":
        _db_watcher = PagerMonDbWatcher(conf.get("pagermon_db_path", ""), _store)
        _db_watcher.start()
    else:
        _watcher = LogWatcher(conf.get("log_file", "/var/log/pagermon/multimon.log"), _store)
        _watcher.start()
    _retry = RetryWorker(_store)
    _retry.start()
    processor.retry_worker = _retry  # let the processor nudge retries
    _cleanup = CleanupWorker(_store)
    _cleanup.start()
    _watchdog = notify.WatchdogNotifier()
    _watchdog.start()
    log.info("pager started; CUPS available=%s", printing.cups_available())


def _apply_runtime_config(conf: dict) -> None:
    """Apply config changes that affect running workers without a restart.

    - ingest_source: start/stop the log vs PagerMon-DB watcher to match.
    - log_file / pagermon_db_path: the active watcher switches path live.
    - output_dir / database / timezone: read fresh per-page, so no action needed.
    """
    global _watcher, _db_watcher
    want = _ingest_source(conf)

    if want == "pagermon_db":
        if _watcher is not None:
            _watcher.stop(); _watcher = None
        path = conf.get("pagermon_db_path", "")
        if _db_watcher is None:
            _db_watcher = PagerMonDbWatcher(path, store()); _db_watcher.start()
        else:
            _db_watcher.set_path(path)
    else:  # log
        if _db_watcher is not None:
            _db_watcher.stop(); _db_watcher = None
        new_log = conf.get("log_file")
        if _watcher is None:
            _watcher = LogWatcher(new_log or "/var/log/pagermon/multimon.log", store())
            _watcher.start()
        elif new_log:
            _watcher.set_path(new_log)


@app.on_event("shutdown")
def _shutdown() -> None:
    for w in (_watcher, _db_watcher, _retry, _cleanup, _watchdog):
        if w:
            w.stop()


# ----------------------------------------------------------------------------- auth
@app.get("/login")
def login_page(request: Request, next: str = "/"):
    # Already authed (or auth off) -> go straight in.
    if not auth.is_enabled() or auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


class LoginBody(BaseModel):
    password: str
    next: str | None = "/"


@app.post("/api/login")
def api_login(body: LoginBody):
    if not auth.is_enabled():
        return JSONResponse({"ok": True, "next": body.next or "/"})
    if not auth.check_password(body.password):
        raise HTTPException(401, "Incorrect password")
    resp = JSONResponse({"ok": True, "next": body.next or "/"})
    resp.set_cookie(
        auth.COOKIE_NAME, auth.make_session_token(),
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/status")
def api_auth_status():
    return {"enabled": auth.is_enabled()}


class PasswordBody(BaseModel):
    new_password: str | None = None
    current_password: str | None = None


@app.post("/api/auth/password")
def api_set_password(body: PasswordBody, request: Request):
    """Set, change, or clear the site password.

    - If a password is already set, the caller must prove it (valid session or
      correct current_password) — prevents a logged-out attacker resetting it.
    - Empty new_password clears auth (disables the gate).
    """
    if auth.is_enabled():
        authed = auth.valid_session(request.cookies.get(auth.COOKIE_NAME))
        if not authed and not auth.check_password(body.current_password or ""):
            raise HTTPException(403, "Current password required")
    new = (body.new_password or "").strip()
    if not new:
        auth.clear_password()
        resp = JSONResponse({"ok": True, "enabled": False})
        resp.delete_cookie(auth.COOKIE_NAME, path="/")
        return resp
    auth.set_password(new)
    # Re-issue a session so the caller isn't immediately locked out by the new hash.
    resp = JSONResponse({"ok": True, "enabled": True})
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session_token(),
                    httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/")
    return resp


# ----------------------------------------------------------------------------- pages
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/layout")
def layout_editor(request: Request):
    return templates.TemplateResponse("layout.html", {"request": request})


@app.get("/rules")
def rules_editor(request: Request):
    return templates.TemplateResponse("rules.html", {"request": request})


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


# ----------------------------------------------------------------------------- jobs API
@app.get("/api/jobs")
def api_jobs(limit: int = 200, offset: int = 0, capcode: str | None = None,
             jobtype: str | None = None, q: str | None = None,
             date_from: str | None = None, date_to: str | None = None,
             printed: str | None = None):
    f = dict(capcode=capcode, jobtype=jobtype, q=q, date_from=date_from,
             date_to=date_to, printed=printed)
    s = store()
    return {
        "jobs": s.query_jobs(limit=limit, offset=offset, **f),
        "total": s.count_jobs(**f),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/jobs.csv")
def api_jobs_csv(capcode: str | None = None, jobtype: str | None = None,
                 q: str | None = None, date_from: str | None = None,
                 date_to: str | None = None, printed: str | None = None):
    """Export the (filtered) job history as CSV for record-keeping."""
    import csv
    import io as _io

    f = dict(capcode=capcode, jobtype=jobtype, q=q, date_from=date_from,
             date_to=date_to, printed=printed)
    jobs = store().query_jobs(limit=100000, offset=0, **f)
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "received_at", "capcode", "alias", "jobtype", "message",
                "printed", "print_failed", "matched_rule", "is_test"])
    for j in jobs:
        w.writerow([j["id"], j["received_at"], j["capcode"],
                    (j.get("fields") or {}).get("capcode_alias", ""),
                    j.get("jobtype") or "", j["message"],
                    int(bool(j["printed"])), int(bool(j.get("print_failed"))),
                    j.get("matched_rule") or "", int(bool(j.get("is_test")))])
    headers = {"Content-Disposition": "attachment; filename=pager_jobs.csv"}
    return Response(buf.getvalue(), media_type="text/csv", headers=headers)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: int):
    job = store().get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}/pdf")
def api_job_pdf(job_id: int):
    job = store().get_job(job_id)
    if not job or not job.get("pdf_path") or not Path(job["pdf_path"]).exists():
        raise HTTPException(404, "PDF not found")
    return FileResponse(job["pdf_path"], media_type="application/pdf")


@app.post("/api/jobs/{job_id}/reprint")
def api_reprint(job_id: int):
    ok, err = reprint_job(job_id, store())
    return JSONResponse({"ok": ok, "error": err}, status_code=200 if ok else 400)


# ----------------------------------------------------------------------------- live events (SSE)
@app.get("/api/events")
async def api_events(request: Request):
    """Server-Sent Events stream: new_job, print_status, print_recovered."""
    q = events.subscribe()

    async def gen():
        try:
            # Initial hello so the client knows the stream is live.
            yield events.sse_format({"event": "hello", "data": {}})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Block in a worker thread so the event loop stays free to
                    # serve other requests (nav clicks, API calls). A bare
                    # q.get(timeout=...) here would block the whole loop.
                    payload = await asyncio.to_thread(q.get, True, 15)
                    yield events.sse_format(payload)
                except Exception:
                    # Heartbeat comment keeps the connection alive through proxies.
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ----------------------------------------------------------------------------- health / watchdog
@app.get("/api/health")
def api_health():
    conf = cfg.load_config()
    max_attempts = configured_max_attempts(conf)
    snap = events.health.snapshot(int(conf.get("watchdog_stale_seconds", 3600)))
    snap["failed_prints"] = store().count_failed_unresolved(max_attempts)
    snap["cups_available"] = printing.cups_available()
    return snap


# ----------------------------------------------------------------------------- print retry
@app.get("/api/prints/failed")
def api_failed_prints():
    max_attempts = configured_max_attempts()
    return {
        "count": store().count_failed_unresolved(max_attempts),
        "jobs": store().list_failed_unresolved(max_attempts),
        "max_attempts": max_attempts,
    }


@app.post("/api/prints/retry-all")
def api_retry_all():
    s = store()
    conf = cfg.load_config()
    max_attempts = configured_max_attempts(conf)
    printer = conf.get("printer_name", "")
    results = []
    for job in s.list_failed_unresolved(max_attempts):
        ok, err = printing.print_pdf(printer, job["pdf_path"], title=f"Retry {job['capcode']}")
        s.update_print_result(job["id"], ok, err, (job.get("print_attempts") or 0) + 1)
        results.append({"job_id": job["id"], "ok": ok, "error": err})
    failed = s.count_failed_unresolved(max_attempts)
    events.publish("print_status", {"failed": failed})
    return {"results": results, "remaining_failed": failed}


# ----------------------------------------------------------------------------- test page injection
class TestPage(BaseModel):
    capcode: str
    message: str


@app.post("/api/test-page")
def api_test_page(body: TestPage):
    job = inject_test_page(store(), body.capcode, body.message)
    if job is None:
        raise HTTPException(400, "Capcode is not in the monitored list — add it in Settings first.")
    return job


# ----------------------------------------------------------------------------- preview (render without storing/printing)
class PreviewReq(BaseModel):
    capcode: str = "0000000"
    message: str = ""


@app.post("/api/preview")
def api_preview(body: PreviewReq):
    """Render a one-off PDF from a sample message using current rules+layout+template."""
    import tempfile

    from .parser import RawPage, apply_rules, build_field_context
    from . import pdfgen

    conf = cfg.load_config()
    rules = cfg.load_rules().get("rules", [])
    layout = cfg.load_layout()
    page = RawPage(capcode=body.capcode, function="0", message=body.message, proto="PREVIEW")
    extracted, _ = apply_rules(page.message, rules)
    alias = (processor.monitored_capcodes(conf).get(body.capcode) or {}).get("label") or None
    ctx = build_field_context(page, extracted, alias=alias, tz_name=conf.get("timezone"))
    out = str(Path(tempfile.gettempdir()) / "pager_preview.pdf")
    pdfgen.render_job_pdf(processor.active_template(conf), layout, ctx, out)
    return FileResponse(out, media_type="application/pdf", filename="preview.pdf")


# ----------------------------------------------------------------------------- retention
@app.post("/api/retention/run")
def api_retention_run():
    assert _cleanup is not None
    return _cleanup.run_once()


# ----------------------------------------------------------------------------- backup / restore
@app.get("/api/settings/export")
def api_settings_export():
    """Download config + rules + layout as one JSON backup."""
    bundle = {
        "version": 1,
        "config": _redact(cfg.load_config()),
        "rules": cfg.load_rules(),
        "layout": cfg.load_layout(),
    }
    headers = {"Content-Disposition": "attachment; filename=pager_settings.json"}
    return Response(json.dumps(bundle, indent=2), media_type="application/json", headers=headers)


@app.post("/api/settings/import")
async def api_settings_import(request: Request):
    """Restore config/rules/layout from an exported bundle. Each section is
    optional, so partial bundles work. Template PDF files are not included."""
    bundle = await request.json()
    if not isinstance(bundle, dict):
        raise HTTPException(400, "Invalid backup file.")
    applied = []
    if isinstance(bundle.get("config"), dict):
        # Preserve the current auth section (the export is redacted), so an
        # import never wipes the password / signing secret.
        new_conf = dict(bundle["config"])
        existing_auth = cfg.load_config().get("auth")
        if existing_auth is not None:
            new_conf["auth"] = existing_auth
        cfg.save_config(new_conf); applied.append("config")
        _apply_runtime_config(new_conf)
    if isinstance(bundle.get("rules"), dict):
        cfg.save_rules(bundle["rules"]); applied.append("rules")
    if isinstance(bundle.get("layout"), dict):
        cfg.save_layout(bundle["layout"]); applied.append("layout")
    if not applied:
        raise HTTPException(400, "Backup contained no config/rules/layout sections.")
    return {"applied": applied}


# ----------------------------------------------------------------------------- config API
def _redact(conf: dict) -> dict:
    """Strip the auth secrets (password hash + signing secret) from a config dict
    before it leaves the server via the API or a settings export."""
    c = dict(conf)
    c.pop("auth", None)
    return c


@app.get("/api/config")
def api_get_config():
    conf = _redact(cfg.load_config())
    # Surface effective defaults for keys a fresh config may omit, so the
    # Settings form always shows a concrete value (single source of truth — the
    # UI doesn't hard-code its own copy of these defaults).
    conf.setdefault("database", "data/jobs.db")
    conf.setdefault("print_max_attempts", retry_mod.DEFAULT_MAX_ATTEMPTS)
    conf.setdefault("print_retry_interval_seconds", retry_mod.DEFAULT_RETRY_INTERVAL_SECONDS)
    return conf


class ConfigPatch(BaseModel):
    global_print_enabled: bool | None = None
    printer_name: str | None = None
    capcodes: list | None = None
    jobtypes: dict | None = None
    template_pdf: str | None = None
    templates: list | None = None
    active_template: str | None = None
    log_file: str | None = None
    ingest_source: str | None = None
    pagermon_db_path: str | None = None
    pagermon_db_mapping: dict | None = None
    output_dir: str | None = None
    database: str | None = None
    print_max_attempts: int | None = None
    print_retry_interval_seconds: int | None = None
    alert_enabled_default: bool | None = None
    watchdog_stale_seconds: int | None = None
    retention_days: int | None = None
    retention_delete_pdf: bool | None = None
    retention_delete_rows: bool | None = None
    timezone: str | None = None
    alert_webhook_url: str | None = None
    poppler_path: str | None = None


@app.patch("/api/config")
def api_patch_config(patch: ConfigPatch):
    conf = cfg.load_config()
    for k, v in patch.model_dump(exclude_none=True).items():
        conf[k] = v
    # The built-in default template can't be deleted: if a templates update
    # dropped it, re-insert it so it always survives (and stays protected).
    if "templates" in patch.model_dump(exclude_none=True):
        conf["templates"] = _preserve_protected_templates(conf.get("templates") or [])
    cfg.save_config(conf)
    # Apply runtime changes that depend on config (watcher path, etc.).
    _apply_runtime_config(conf)
    return conf


def _preserve_protected_templates(incoming: list) -> list:
    """Ensure the protected default template stays in the list even if a client
    submitted a templates array without it (or stripped its protected flag)."""
    saved = cfg.load_config().get("templates") or []
    protected = [t for t in saved if t.get("protected")]
    out = list(incoming)
    for p in protected:
        match = next((t for t in out if t.get("name") == p.get("name")), None)
        if match is None:
            out.insert(0, p)               # was deleted — put it back
        else:
            match["path"] = p["path"]       # keep its real path
            match["protected"] = True       # keep it protected
    return out


# ----------------------------------------------------------------------------- layout API
@app.get("/api/layout")
def api_get_layout():
    return cfg.load_layout()


@app.put("/api/layout")
async def api_put_layout(request: Request):
    data = await request.json()
    cfg.save_layout(data)
    return data


@app.get("/api/template/status")
def api_template_status():
    """Whether a preview image is available, so the layout editor can decide to
    load /api/template/image or fall back to a blank page (no 404 request)."""
    conf = cfg.load_config()
    tpl = processor.active_template(conf)
    exists = bool(tpl) and Path(tpl).exists()
    # A preview image needs both the pdf2image package AND the poppler binary.
    try:
        import pdf2image  # noqa: F401
        have_pkg = True
    except Exception:  # noqa: BLE001
        have_pkg = False
    have_poppler = poppler.is_available()
    # The template's real page size, so the editor can use the SAME coordinate
    # space the renderer merges onto (avoids Letter-vs-A4 misalignment).
    page_w = page_h = None
    if exists:
        try:
            from pypdf import PdfReader
            box = PdfReader(tpl).pages[0].mediabox
            page_w, page_h = float(box.width), float(box.height)
        except Exception:  # noqa: BLE001
            pass
    return {
        "template_path": tpl or "",
        "exists": exists,
        "poppler_available": have_poppler,
        "image_available": exists and have_pkg and have_poppler,
        "page_width": page_w,
        "page_height": page_h,
    }


@app.post("/api/template/upload")
async def api_template_upload(file: UploadFile = File(...)):
    """Upload a template PDF. Saves it under <config>/templates/ and returns the
    stored path for use in a template entry."""
    import re as _re

    name = file.filename or "template.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only .pdf files are accepted.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 25 MB).")
    # Cheap sanity check: real PDFs start with the %PDF- signature.
    if not data[:5] == b"%PDF-":
        raise HTTPException(400, "That doesn't look like a PDF (missing %PDF header).")

    # Sanitize to a safe filename: take the basename only (defeats ../ and
    # absolute paths), allow a conservative charset, strip leading dots.
    base = Path(name.replace("\\", "/")).name           # drop any directory parts
    stem = _re.sub(r"[^A-Za-z0-9._-]", "_", Path(base).stem).lstrip(".") or "template"
    dest_dir = (cfg.CONFIG_DIR / "templates").resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}.pdf"
    # Avoid clobbering an existing different template: add -1, -2, … if needed.
    i = 1
    while dest.exists():
        dest = dest_dir / f"{stem}-{i}.pdf"
        i += 1
    # Defense in depth: the resolved destination must stay within templates/.
    if dest_dir not in dest.resolve().parents:
        raise HTTPException(400, "Invalid filename.")
    dest.write_bytes(data)
    return {"path": str(dest), "name": stem, "size": len(data)}


@app.get("/api/template/image")
def api_template_image():
    """Render page 1 of the template PDF to PNG for the layout editor canvas."""
    conf = cfg.load_config()
    tpl = processor.active_template(conf)
    if not tpl or not Path(tpl).exists():
        raise HTTPException(404, "Template PDF not found")
    try:
        from pdf2image import convert_from_path

        # Pass an explicit poppler dir when it isn't on PATH (e.g. Windows).
        kwargs = {}
        pdir = poppler.poppler_dir()
        if pdir:
            kwargs["poppler_path"] = pdir
        images = convert_from_path(tpl, first_page=1, last_page=1, dpi=100, **kwargs)
        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not render template (is poppler installed?): {exc}")


# ----------------------------------------------------------------------------- text metrics
class MeasureItem(BaseModel):
    text: str = ""
    font: str = "Helvetica"
    size: float = 11
    max_width: float = 0


@app.post("/api/measure-text")
def api_measure_text(items: list[MeasureItem]):
    """Wrap each item's text using reportlab's real font metrics — identical to
    pdfgen — so the layout editor can render the exact same lines/breaks as the
    printed PDF. Returns, per item, the wrapped lines and each line's width (pt)."""
    out = []
    for it in items:
        lines = pdfgen.wrap_text(it.text, it.font, it.size, it.max_width)
        out.append({
            "lines": lines,
            "widths": [pdfgen.text_width(ln, it.font, it.size) for ln in lines],
        })
    return out


# ----------------------------------------------------------------------------- fields API
@app.get("/api/fields")
def api_fields():
    """Placeable PDF fields: built-ins + every field the current rules produce.

    The layout editor uses this so you can only place fields that a rule will
    actually fill (plus the always-available built-ins)."""
    rules = cfg.load_rules().get("rules", [])
    return {
        "builtins": parsermod.BUILTIN_FIELDS,
        "fields": parsermod.available_fields(rules),
    }


# ----------------------------------------------------------------------------- rules API
@app.get("/api/rules")
def api_get_rules():
    return cfg.load_rules()


@app.put("/api/rules")
async def api_put_rules(request: Request):
    data = await request.json()
    # Keep the stored YAML tidy: a rule may carry a `groups` map ({group#: field})
    # when authored in the plain-regex + assign-fields mode. Drop it when empty so
    # legacy/simple rules stay minimal. The parser expands `pattern` + `groups`
    # into named groups at match time (parser.effective_pattern).
    for rule in data.get("rules", []) or []:
        groups = {k: v for k, v in (rule.get("groups") or {}).items() if v}
        if groups:
            rule["groups"] = groups
        else:
            rule.pop("groups", None)
    cfg.save_rules(data)
    return data


class TestRule(BaseModel):
    message: str
    # Optional: test the rules currently being edited (unsaved). When omitted,
    # the saved rules are used.
    rules: list | None = None


@app.post("/api/rules/test")
def api_test_rules(body: TestRule):
    """Diagnose how the (posted or saved) rules apply to a sample message.

    Returns the winning rule + extracted fields, plus a per-rule breakdown with
    match/group spans and regex errors so the layout author can see exactly what
    each rule captures."""
    rules = body.rules if body.rules is not None else cfg.load_rules().get("rules", [])
    return parsermod.diagnose_rules(body.message, rules)


# ----------------------------------------------------------------------------- PagerMon DB source
class ProbeReq(BaseModel):
    path: str | None = None
    mapping: dict | None = None


@app.post("/api/pagermon-db/probe")
def api_pagermon_db_probe(body: ProbeReq):
    """Inspect a PagerMon SQLite DB and report the auto-detected column mapping,
    plus a small sample of recent rows as they'd be ingested. Drives the
    Settings -> PagerMon DB panel so a user can confirm or override the mapping."""
    conf = cfg.load_config()
    path = (body.path if body.path is not None else conf.get("pagermon_db_path")) or ""
    override = body.mapping if body.mapping is not None else conf.get("pagermon_db_mapping")
    m = pagermon_db.probe_schema(path, override)
    sample = []
    if m.detected:
        try:
            latest = pagermon_db.latest_id(path, m)
            # Pull the few most-recent rows: read from just below the high id.
            rows, _ = pagermon_db.fetch_new(path, m, max(0, latest - 5))
            for _rid, page, alias in rows[-5:]:
                sample.append({
                    "capcode": page.capcode,
                    "message": page.message[:120],
                    "received_at": page.received_at.isoformat(timespec="seconds"),
                    "alias": alias,
                })
        except Exception as exc:  # noqa: BLE001
            m.note += f" (sample failed: {exc})"
    return {"path": path, "mapping": m.to_dict(), "sample": sample}


# ----------------------------------------------------------------------------- printers API
@app.get("/api/printers")
def api_printers():
    return {
        "cups_available": printing.cups_available(),
        "installed": printing.list_printers(),
    }


@app.get("/api/printers/discover")
def api_discover():
    return {"devices": printing.discover_network_printers()}


class AddPrinter(BaseModel):
    name: str
    uri: str


@app.post("/api/printers/add")
def api_add_printer(body: AddPrinter):
    ok, msg = printing.add_printer(body.name, body.uri)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)
