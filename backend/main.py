from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Load backend/.env before any module reads os.getenv()
# override=True ensures a key change in .env takes effect on restart
# even if GEMINI_API_KEY was already set in the shell environment.
load_dotenv(override=True)

from pipeline import detector, inpainter, ocr, splitter, translator, typesetter
from core.job_manager import JobManager
from core.manga_downloader import download_chapter, extract_chapter_id
from core.pdf_utils import build_compressed_pdf
from core.cache import TTLCache
from core.ratelimit import RateLimiter, client_ip
from core import library

app = FastAPI(title="Hebrew Manga Translator API", version="0.1.0")

# ── CORS ────────────────────────────────────────────────────────────────────
# Lock origins down in production via ALLOWED_ORIGINS (comma-separated list of
# exact origins, e.g. "https://my-app.vercel.app,https://www.my-app.com").
# Defaults to "*" for local dev. We never use cookies/sessions (auth is via
# X-* headers / body keys), so allow_credentials stays False — which also keeps
# a wildcard origin valid per the CORS spec.
_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],          # includes X-Gemini-Api-Key, X-Modal-Token-Id/Secret
)

JOBS_DIR = Path("data/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = 200
ALLOWED_EXTENSIONS = {".pdf", ".zip"}

job_manager = JobManager()

# ── GPU policy ───────────────────────────────────────────────────────────────
# Detection + inpainting ALWAYS run on Modal GPU, paid for by each user's own
# Modal tokens (BYOK). There is intentionally no server-side "use local GPU"
# escape hatch: the production host is small and cannot run these models, so
# every job MUST supply Modal tokens (enforced in _require_api_keys below).

# ── Concurrency control ──────────────────────────────────────────────────────
# Cap how many pipelines run their heavy stages at once. Even though detection/
# inpainting are offloaded to Modal, each job still does local work (page
# downloads, PDF assembly, the EasyOCR fallback) — so unbounded concurrency
# would exhaust the small host. Extra jobs are accepted and queue for a slot.
_MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))
_pipeline_sem = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)

# One active translation per client IP. Maps client IP -> job_id. Prevents a
# single visitor from queuing dozens of jobs and starving everyone else.
_active_job_ips: dict[str, str] = {}

# ── Periodic janitor ─────────────────────────────────────────────────────────
# Sweeps data/jobs/ for abandoned/incomplete jobs while the server stays up for
# long stretches (production: no restart-triggered scan).
_JANITOR_INTERVAL_SECONDS = int(os.getenv("JANITOR_INTERVAL_SECONDS", str(3 * 60 * 60)))  # every 3 h
_JANITOR_GRACE_SECONDS    = int(os.getenv("JANITOR_GRACE_SECONDS", str(60 * 60)))          # 60 min idle

# ── Caching + rate limiting (public, no-auth endpoints) ──────────────────────
_cache = TTLCache()
# Tight limit for endpoints that scrape WeebCentral (protects our IP from being
# throttled/blocked upstream); roomier limit for cheap local DB reads.
_scrape_rl = RateLimiter(calls=int(os.getenv("SCRAPE_RATE_PER_MIN", "30")), window=60)
_api_rl    = RateLimiter(calls=int(os.getenv("API_RATE_PER_MIN", "120")),  window=60)


# ---------------------------------------------------------------------------
# Per-IP active-job reservation helpers
# ---------------------------------------------------------------------------

def _reserve_ip_slot(ip: str, job_id: str) -> None:
    """
    Claim the single active-job slot for this client IP, or reject with 429 if
    the IP already has a translation in progress. Released by _release_ip_slot
    in the pipeline runner's finally block (and reset automatically on restart
    since the map lives in process memory).
    """
    if ip in _active_job_ips:
        raise HTTPException(
            status_code=429,
            detail="You already have a translation in progress. "
                   "Please wait for it to finish before starting another.",
        )
    _active_job_ips[ip] = job_id


def _release_ip_slot(job_id: str) -> None:
    """Free whichever IP slot is holding this job_id (no-op if already gone)."""
    for ip, jid in list(_active_job_ips.items()):
        if jid == job_id:
            _active_job_ips.pop(ip, None)


# ---------------------------------------------------------------------------
# Health check (used by the hosting platform's uptime probe)
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/healthz")
async def health() -> dict:
    """Liveness probe — returns current capacity + library mode."""
    return {
        "status":        "ok",
        "active_jobs":   len(_active_job_ips),
        "max_concurrent": _MAX_CONCURRENT_JOBS,
        "library_mode":  "cloud" if library._supabase_mode() else
                         ("hybrid" if library._r2_mode() else "local"),
    }


# ---------------------------------------------------------------------------
# Startup: scan completed jobs and register any that are not yet in the library
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup_scan_library() -> None:
    """
    On every server start, walk data/jobs/ and:

      1. Delete any job directory that never reached completion — i.e. has
         neither output/result.pdf nor output/result_compressed.pdf. These
         are jobs that were interrupted (server crash/restart mid-pipeline,
         or a process that died before its own except-block cleanup ran) and
         would otherwise sit on disk forever as orphaned data.

      2. For completed jobs that have a chapter_meta.json (URL-based jobs
         with library metadata) and are NOT already in the library DB
         (checked by mangadex_id), register them.

    Step 2 makes library registration resilient to server restarts that
    killed the background task before it could commit.
    """
    registered = skipped = failed = deleted = 0
    r2_mode = library._r2_mode()
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue

        # Accept either result.pdf or result_compressed.pdf as completion markers
        output_dir    = job_dir / "output"
        pdf_path      = output_dir / "result_compressed.pdf"
        pdf_path_full = output_dir / "result.pdf"
        if not pdf_path.exists() and not pdf_path_full.exists():
            # Interrupted job — never produced a final output. Remove it so
            # incomplete jobs don't accumulate on the server.
            shutil.rmtree(job_dir, ignore_errors=True)
            job_manager.remove_job(job_dir.name)
            deleted += 1
            continue

        meta_path = job_dir / "chapter_meta.json"
        if not meta_path.exists():
            # Completed file-upload job (no library metadata) — keep as-is.
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            mangadex_id = meta.get("mangadex_id", "")
            if not mangadex_id:
                continue
            cached = await library.check_cache(mangadex_id)
            if cached:
                # If R2 is now active but the stored URL is a local /api/ path,
                # re-register to upgrade it to a permanent R2 URL.
                if r2_mode and str(cached.get("pdf_url", "")).startswith("/api/"):
                    await _register_in_library(job_dir)
                    registered += 1
                else:
                    skipped += 1
                continue
            await _register_in_library(job_dir)
            registered += 1
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "[startup] Failed to register job %s: %s", job_dir.name, exc
            )
            failed += 1

    if registered or failed or deleted:
        import logging as _log
        _log.getLogger(__name__).info(
            "[startup] Library scan: %d registered, %d skipped (already in DB), "
            "%d failed, %d incomplete job(s) deleted",
            registered, skipped, failed, deleted,
        )


# ---------------------------------------------------------------------------
# Periodic janitor — for long-lived production servers that never restart
# ---------------------------------------------------------------------------
#
# The startup scan above only runs once, when the process boots. On a
# production deploy that stays up for days/weeks with many concurrent users,
# a job can become permanently abandoned without ever raising an exception
# (e.g. a Gemini/Modal call that hangs instead of erroring, a deadlocked
# inpainting step, etc.) — in that case the per-job try/except cleanup in
# _run_pipeline* never fires, and the directory would sit on disk forever.
#
# This loop wakes up every _JANITOR_INTERVAL_SECONDS (default 3 h) and
# deletes any job directory that is:
#   • incomplete         — no output/result.pdf or output/result_compressed.pdf
#   • not active         — job_manager has no in-progress record for it, so a
#                           job genuinely being worked on right now (by this
#                           or any concurrent user) is NEVER touched
#   • idle long enough   — nothing under the directory has been modified in
#                           the last _JANITOR_GRACE_SECONDS (default 30 min),
#                           so a job that just started is never mistaken for
#                           abandoned

async def _janitor_sweep() -> None:
    """Delete abandoned/incomplete job directories. See module note above."""
    deleted = 0
    now = time.time()

    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue

        output_dir    = job_dir / "output"
        pdf_path      = output_dir / "result_compressed.pdf"
        pdf_path_full = output_dir / "result.pdf"
        if pdf_path.exists() or pdf_path_full.exists():
            continue  # completed — keep

        job_id = job_dir.name
        if job_id in job_manager._history and not job_manager._done.get(job_id, False):
            continue  # actively being processed right now — never touch

        try:
            newest = max(
                (p.stat().st_mtime for p in job_dir.rglob("*")),
                default=job_dir.stat().st_mtime,
            )
        except OSError:
            continue  # directory vanished mid-scan or unreadable — skip safely

        if now - newest < _JANITOR_GRACE_SECONDS:
            continue  # still fresh — give it more time to finish or fail naturally

        shutil.rmtree(job_dir, ignore_errors=True)
        job_manager.remove_job(job_id)
        deleted += 1

    if deleted:
        import logging as _log
        _log.getLogger(__name__).info(
            "[janitor] Removed %d abandoned/incomplete job director%s",
            deleted, "y" if deleted == 1 else "ies",
        )


async def _janitor_loop() -> None:
    """Run _janitor_sweep() forever, every _JANITOR_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(_JANITOR_INTERVAL_SECONDS)
        try:
            await _janitor_sweep()
        except Exception:
            import logging as _log
            _log.getLogger(__name__).exception("[janitor] Sweep failed")


@app.on_event("startup")
async def _start_janitor() -> None:
    """Kick off the periodic janitor loop as a background task."""
    asyncio.create_task(_janitor_loop())


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class FetchChapterBody(BaseModel):
    url:               str
    data_saver:        bool = False
    # API keys can be sent in the body (preferred) or as X-* headers (fallback).
    # Body keys are more reliable — some proxies/CORS pre-flight strips custom headers.
    gemini_api_key:    str | None = None
    modal_token_id:    str | None = None
    modal_token_secret: str | None = None


_RESUME_STEPS = {"detect", "ocr", "inpaint", "translate", "typeset"}

class ResumeBody(BaseModel):
    from_step: str = "translate"   # one of: detect | ocr | inpaint | translate | typeset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _require_job(job_id: str) -> Path:
    job_dir = _job_dir(job_id)
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found.")
    return job_dir


def _create_job_dirs(job_id: str) -> Path:
    """Create and return the job directory with all required subdirectories."""
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True)
    for sub in ("original", "detection", "cleaned", "translated", "output"):
        (job_dir / sub).mkdir()
    return job_dir


def _save_job_config(
    job_dir:            Path,
    request:            Request,
    gemini_api_key:     str | None = None,
    modal_token_id:     str | None = None,
    modal_token_secret: str | None = None,
) -> None:
    """
    Persist per-job API credentials to job_config.json.

    Keys can arrive two ways (body-supplied args take priority over headers):
      - As JSON body fields (FetchChapterBody.gemini_api_key / modal_token_*)
      - As request headers X-Gemini-Api-Key / X-Modal-Token-Id / X-Modal-Token-Secret

    All keys live only in job_config.json and are never stored in any database.
    """
    config: dict = {}

    # Prefer explicitly-passed values (from JSON body); fall back to headers.
    gemini_key = (gemini_api_key or "").strip() or \
                 (request.headers.get("X-Gemini-Api-Key") or "").strip()
    if gemini_key:
        config["gemini_api_key"] = gemini_key

    mid = (modal_token_id     or "").strip() or \
          (request.headers.get("X-Modal-Token-Id") or "").strip()
    sec = (modal_token_secret or "").strip() or \
          (request.headers.get("X-Modal-Token-Secret") or "").strip()
    if mid and sec:
        config["modal_token_id"]     = mid
        config["modal_token_secret"] = sec

    (job_dir / "job_config.json").write_text(
        json.dumps(config, ensure_ascii=False),
        encoding="utf-8",
    )


def _require_api_keys(request: Request, body: "FetchChapterBody | None" = None) -> None:
    """
    Hard gate: every job creation must supply BOTH a Gemini API key AND Modal
    GPU tokens (BYOK). Detection/inpainting always run on the user's own Modal
    GPU — there is no server-side fallback — so the tokens are non-negotiable.

    Checks body fields first (JSON body), then X-* headers as fallback.
    Raises HTTPException 422 if any required credential is absent.
    """
    gemini_key = (
        (getattr(body, "gemini_api_key", None) or "").strip()
        or (request.headers.get("X-Gemini-Api-Key") or "").strip()
    )
    if not gemini_key:
        raise HTTPException(
            status_code=422,
            detail="A Gemini API key is required. Add it in Settings.",
        )

    modal_id = (
        (getattr(body, "modal_token_id", None) or "").strip()
        or (request.headers.get("X-Modal-Token-Id") or "").strip()
    )
    modal_sec = (
        (getattr(body, "modal_token_secret", None) or "").strip()
        or (request.headers.get("X-Modal-Token-Secret") or "").strip()
    )
    if not (modal_id and modal_sec):
        raise HTTPException(
            status_code=422,
            detail="Modal GPU tokens (Token ID + Token Secret) are required. Add them in Settings.",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/jobs", status_code=201)
async def create_job(
    request:            Request,
    file:               UploadFile = File(...),
    gemini_api_key:     str | None = Form(default=None),
    modal_token_id:     str | None = Form(default=None),
    modal_token_secret: str | None = Form(default=None),
):
    """Accept a .pdf or .zip upload, spin up the translation pipeline.

    API keys can be provided as form fields (preferred — more reliable than
    headers for multipart/form-data) or as X-* request headers (fallback).
    """
    # Build a lightweight body-like object so _require_api_keys can check form
    # fields with the same logic it uses for the JSON body on from-url jobs.
    class _FormKeys:
        pass
    _form_body = _FormKeys()
    _form_body.gemini_api_key    = gemini_api_key     # type: ignore[attr-defined]
    _form_body.modal_token_id    = modal_token_id      # type: ignore[attr-defined]
    _form_body.modal_token_secret = modal_token_secret # type: ignore[attr-defined]

    _require_api_keys(request, _form_body)  # type: ignore[arg-type]

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{suffix}'. Upload a .pdf or .zip.",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit.",
        )

    job_id  = str(uuid.uuid4())
    job_dir = _create_job_dirs(job_id)
    _save_job_config(
        job_dir, request,
        gemini_api_key=gemini_api_key,
        modal_token_id=modal_token_id,
        modal_token_secret=modal_token_secret,
    )

    upload_path = job_dir / f"source{suffix}"
    upload_path.write_bytes(content)

    # Enforce one active translation per IP, then hand off to the background
    # runner (which releases the slot when the job ends, succeed or fail).
    _reserve_ip_slot(client_ip(request), job_id)
    job_manager.register_job(job_id)
    asyncio.create_task(_run_pipeline(job_id, upload_path))

    return {"job_id": job_id}


@app.post("/api/jobs/from-url", status_code=201)
async def create_job_from_url(request: Request, body: FetchChapterBody):
    """
    Download a MangaDex chapter by URL or UUID, then run the translation pipeline.

    Body JSON:
      { "url": "https://mangadex.org/chapter/<uuid>", "data_saver": false }

    data_saver: set true for lower-resolution images (faster download, smaller file).

    If the chapter has already been translated (library cache hit), returns:
      { "job_id": null, "cached": true, "library_id": "<uuid>" }
    and no pipeline is started.  The frontend should redirect to /library/<library_id>.
    """
    _require_api_keys(request, body)

    # ── Library cache check ────────────────────────────────────────────────────
    try:
        mangadex_id = extract_chapter_id(body.url)
        cached = await library.check_cache(mangadex_id)
        if cached:
            return {"job_id": None, "cached": True, "library_id": cached["id"]}
    except ValueError:
        pass   # URL parse failure — let the pipeline give a proper error

    job_id  = str(uuid.uuid4())
    job_dir = _create_job_dirs(job_id)
    _save_job_config(
        job_dir, request,
        gemini_api_key=body.gemini_api_key,
        modal_token_id=body.modal_token_id,
        modal_token_secret=body.modal_token_secret,
    )

    _reserve_ip_slot(client_ip(request), job_id)
    job_manager.register_job(job_id)
    asyncio.create_task(
        _run_pipeline_from_url(job_id, job_dir, body.url, body.data_saver)
    )

    return {"job_id": job_id, "cached": False}


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    """SSE stream of pipeline progress events."""
    _require_job(job_id)

    async def generate():
        async for chunk in job_manager.subscribe(job_id):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/download")
async def download_result(job_id: str, compressed: bool = False):
    """
    Stream the finished translated PDF.

    ?compressed=true   — re-encodes pages as JPEG (quality 85) before PDF assembly.
                         Typically reduces file size by 60-80 % with minimal visual loss.
                         The compressed PDF is cached on disk after the first request.
    """
    job_dir = _require_job(job_id)
    output_dir = job_dir / "output"

    if compressed:
        result = output_dir / "result_compressed.pdf"
        if not result.exists():
            full = output_dir / "result.pdf"
            if not full.exists():
                raise HTTPException(status_code=404, detail="Result not ready yet.")
            # Try to generate a compressed PDF from the page images.
            # If the pages were already cleaned up after R2 upload, fall back
            # to serving the full-quality PDF so the download still works.
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _build_compressed_pdf, output_dir, result)
            except Exception:
                # Page images were cleaned up (e.g. after R2 upload) — serve full PDF
                return FileResponse(
                    full,
                    media_type="application/pdf",
                    filename="translated_manga_compressed.pdf",
                )
        return FileResponse(
            result,
            media_type="application/pdf",
            filename="translated_manga_compressed.pdf",
        )

    result = output_dir / "result.pdf"
    if not result.exists():
        raise HTTPException(status_code=404, detail="Result not ready yet.")
    return FileResponse(result, media_type="application/pdf", filename="translated_manga.pdf")


def _build_compressed_pdf(output_dir: Path, dest: Path, quality: int = 85) -> None:
    """Thin wrapper kept for the download endpoint; real logic lives in core/pdf_utils.py."""
    build_compressed_pdf(output_dir, dest, quality=quality, save_pages=False)


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Remove all job artifacts from disk."""
    job_dir = _require_job(job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    job_manager.remove_job(job_id)


@app.post("/api/jobs/{job_id}/resume", status_code=202)
async def resume_job(job_id: str, body: ResumeBody, request: Request):
    """
    Re-run the pipeline from a given step using already-computed artifacts.

    Useful during development to iterate on translate/typeset without
    re-running the slow detect/OCR/inpaint steps.

    Body JSON:  { "from_step": "translate" }
    Valid steps: detect | ocr | inpaint | translate | typeset

    The job directory must already exist (i.e. the job was created previously).
    Output from earlier steps on disk is reused as-is.
    """
    job_dir = _require_job(job_id)

    if body.from_step not in _RESUME_STEPS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid from_step '{body.from_step}'. "
                   f"Must be one of: {', '.join(sorted(_RESUME_STEPS))}",
        )

    pages = _load_existing_pages(job_dir)
    if not pages:
        raise HTTPException(
            status_code=409,
            detail="No pages found in original/. "
                   "Run a full job first before resuming.",
        )

    # Hold the same one-job-per-IP slot for the resumed run.
    _reserve_ip_slot(client_ip(request), job_id)

    # Salvage chapter title before history is cleared
    title_file = job_dir / "chapter_title.txt"
    chapter_title = (
        title_file.read_text(encoding="utf-8").strip()
        if title_file.exists() else None
    )
    # Fallback: scan old in-memory history (works if server hasn't restarted)
    if not chapter_title:
        for ev in job_manager._history.get(job_id, []):
            chapter_title = ev.get("chapter_title") or ev.get("chapter")
            if chapter_title:
                break

    # Re-register clears history and disconnects live subscribers
    job_manager.register_job(job_id)
    emit = job_manager.get_emitter(job_id)

    # Pre-populate history with synthetic "done" events for every stage that
    # ran before from_step.  When the frontend connects (even mid-pipeline) it
    # replays these and correctly shows earlier stages as completed.
    total = len(pages)
    _ordered = ["detect", "ocr", "inpaint", "translate", "typeset"]
    start_idx = _ordered.index(body.from_step)

    await emit({
        "stage": "download", "status": "done", "total_pages": total,
        **({"chapter_title": chapter_title} if chapter_title else {}),
    })
    for step in _ordered[:start_idx]:
        await emit({"stage": step, "status": "done", "total_pages": total})

    asyncio.create_task(
        _run_pipeline_from_step(job_id, job_dir, pages, body.from_step)
    )

    return {"job_id": job_id, "resuming_from": body.from_step}


# ---------------------------------------------------------------------------
# Modal BYOK — one-time deploy to user's workspace
# ---------------------------------------------------------------------------

class ModalSetupBody(BaseModel):
    modal_token_id:     str
    modal_token_secret: str


@app.post("/api/modal/setup")
async def modal_setup(body: ModalSetupBody):
    """
    Deploy modal_gpu.py to the user's own Modal workspace using their credentials.

    This is a one-time setup step (~45 s).  After it completes the user's
    account owns the deployed app and all GPU costs are billed to them.
    The credentials are NOT stored server-side; the client saves them in
    localStorage and sends them as headers on every pipeline job request.
    """
    tid = body.modal_token_id.strip()
    sec = body.modal_token_secret.strip()
    if not tid or not sec:
        raise HTTPException(422, "Both modal_token_id and modal_token_secret are required.")

    modal_script = Path(__file__).parent / "modal_gpu.py"
    if not modal_script.exists():
        raise HTTPException(500, "modal_gpu.py not found on the server.")

    deploy_env = {
        **os.environ,
        "MODAL_TOKEN_ID":   tid,
        "MODAL_TOKEN_SECRET": sec,
        # Force UTF-8 I/O so the Modal CLI can print its box-drawing characters
        # on Windows without hitting a charmap UnicodeEncodeError.
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8":       "1",
    }

    # asyncio.create_subprocess_exec raises NotImplementedError on Windows with
    # SelectorEventLoop (used by uvicorn).  Run the blocking subprocess.run call
    # in a thread instead — safe on all platforms, keeps the event loop free.
    def _run_deploy() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "modal", "deploy", str(modal_script)],
            env=deploy_env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",   # replace any remaining undecodable bytes rather than crash
        )

    try:
        result = await asyncio.to_thread(_run_deploy)
    except FileNotFoundError:
        raise HTTPException(500, "modal package not found in this Python environment. Run: pip install modal")

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise HTTPException(500, f"Modal deploy failed: {output[-2000:]}")

    return {"success": True}


# ---------------------------------------------------------------------------
# Intermediate-file cleanup
# ---------------------------------------------------------------------------

async def _cleanup_intermediates(job_dir: Path) -> None:
    """
    Delete all intermediate pipeline artifacts after translation completes.
    Only the output/ folder (compressed PDF + page JPEGs) is kept.
    Runs in a thread so file I/O doesn't block the event loop.
    """
    def _do_cleanup() -> None:
        for subdir in ("original", "detection", "cleaned", "translated"):
            shutil.rmtree(job_dir / subdir, ignore_errors=True)
        for fname in ("source.pdf", "source.zip"):
            p = job_dir / fname
            if p.exists():
                p.unlink(missing_ok=True)

    await asyncio.get_running_loop().run_in_executor(None, _do_cleanup)


# ---------------------------------------------------------------------------
# Pipelined orchestrator — per-page parallel pipeline (Steps 1–5)
# ---------------------------------------------------------------------------

async def _run_pipeline_steps(
    job_id:  str,
    job_dir: Path,
    pages:   list[Path],
    emit,
) -> None:
    """
    Run Steps 1–5 of the pipeline in strict stage-by-stage order.

    All pages complete each stage before the next stage begins:
      detect ALL pages → ocr ALL pages → inpaint ALL pages
      → translate ALL pages → typeset ALL pages

    Each stage module handles its own per-page progress events and emits
    its own 'done' event (including cost/modal_gpu_seconds metadata).
    """
    cfg_path = job_dir / "job_config.json"
    job_cfg  = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    modal_token_id:     str | None = job_cfg.get("modal_token_id")    or None
    modal_token_secret: str | None = job_cfg.get("modal_token_secret") or None

    await detector.detect(job_dir, pages, emit, modal_token_id, modal_token_secret)
    await ocr.ocr(job_dir, pages, emit)
    await inpainter.inpaint(job_dir, pages, emit, modal_token_id, modal_token_secret)
    await translator.translate(job_dir, pages, emit)
    await typesetter.typeset(job_dir, pages, emit)

    await emit({
        "stage":        "done",
        "total_pages":  len(pages),
        "download_url": f"/api/jobs/{job_id}/download",
    })


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------

async def _run_pipeline(job_id: str, source_file: Path) -> None:
    """Pipeline runner for file-upload jobs (PDF / ZIP source)."""
    emit    = job_manager.get_emitter(job_id)
    job_dir = _job_dir(job_id)

    try:
        # Gate heavy work behind the global concurrency cap. The job was already
        # accepted (201) so the SSE stream is connected; it simply waits here
        # until a slot frees up.
        async with _pipeline_sem:
            # ── Step 0: Split ──────────────────────────────────────────────
            pages = await splitter.split(job_dir, source_file, emit)

            await _run_pipeline_steps(job_id, job_dir, pages, emit)

        # ── Cleanup: remove intermediate dirs to save disk space ──────────
        asyncio.create_task(_cleanup_intermediates(job_dir))

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
        # Delete the failed job directory so it doesn't accumulate on disk.
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    finally:
        _release_ip_slot(job_id)


async def _run_pipeline_from_url(
    job_id:     str,
    job_dir:    Path,
    url:        str,
    data_saver: bool,
) -> None:
    """Pipeline runner for MangaDex URL jobs."""
    emit = job_manager.get_emitter(job_id)

    try:
        # Gate heavy work behind the global concurrency cap (see _run_pipeline).
        async with _pipeline_sem:
            # ── Step 0: Download ───────────────────────────────────────────
            pages = await download_chapter(url, job_dir, emit, data_saver=data_saver)

            await _run_pipeline_steps(job_id, job_dir, pages, emit)

            # ── Cleanup: remove intermediate dirs to save disk space ──────
            asyncio.create_task(_cleanup_intermediates(job_dir))

        # ── Library: build compressed PDF + register chapter ──────────────
        # Done outside the semaphore so the R2 upload doesn't hold a slot that
        # another queued job could use. "done" was already emitted inside
        # _run_pipeline_steps, so awaiting registration here doesn't block the
        # user from getting their link. We await (rather than create_task) so
        # the write commits before the coroutine returns — a server restart no
        # longer loses the entry.
        await _register_in_library(job_dir, emit=emit)

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
        # Delete the failed job directory so it doesn't accumulate on disk.
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    finally:
        _release_ip_slot(job_id)


# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

async def _register_in_library(job_dir: Path, emit=None) -> None:
    """
    Upload output files to R2 and register the chapter in Supabase.

    Silently skipped when library is disabled.  Errors are logged but never
    re-raised — the user already received their download link.

    If `emit` is provided, fires a "library_ready" event with the new library_id
    so the job-progress page can show a "Read in Hebrew" button immediately.
    """
    if not library.library_enabled():
        return

    meta_path = job_dir / "chapter_meta.json"
    if not meta_path.exists():
        return   # file-upload job (no MangaDex metadata)

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        mangadex_id = meta.get("mangadex_id", "")
        if not mangadex_id:
            return

        pdf_url, pages_prefix, page_count, pdf_size_kb = (
            await library.upload_chapter_files(job_dir, mangadex_id)
        )

        library_id = await library.register_chapter(
            mangadex_id   = mangadex_id,
            manga_title   = meta.get("manga_title", "Unknown"),
            manga_id      = meta.get("manga_id", ""),
            chapter_num   = meta.get("chapter_num", ""),
            chapter_title = meta.get("chapter_title", ""),
            cover_url     = meta.get("cover_url", ""),
            page_count    = page_count,
            pdf_url       = pdf_url,
            pages_prefix  = pages_prefix,
            pdf_size_kb   = pdf_size_kb,
        )

        # Notify connected SSE subscribers that the reader is ready
        if emit and library_id:
            await emit({"stage": "library_ready", "library_id": library_id})

    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[library] _register_in_library failed: %s", exc)


# ---------------------------------------------------------------------------
# WeebCentral proxy  (avoids CORS — browser can't call weebcentral.com directly)
# ---------------------------------------------------------------------------

_WC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html, application/json, */*",
    "Referer": "https://weebcentral.com/",
}

import re as _re
# WeebCentral real series IDs look like ULID: 26 uppercase alphanumeric chars.
# This rejects navigation slugs like "random", "popular", "latest", etc.
_WC_SERIES_ID_RE = _re.compile(r'^[0-9A-HJKMNP-TV-Z]{26}$', _re.I)


def _parse_wc_series(html_text: str, *, limit: int = 30) -> list[dict]:
    """
    Parse WeebCentral HTML response and return a clean list of
    { id, title, cover, url } dicts, deduplicated and validated.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        href: str = link.get("href", "")
        if "/series/" not in href:
            continue

        # Extract series ID and validate it's a real ULID (not "random" etc.)
        series_id = href.rstrip("/").split("/series/")[-1].split("/")[0]
        if not series_id or series_id in seen:
            continue
        if not _WC_SERIES_ID_RE.match(series_id):
            continue  # skip slugs like "random", "popular", navigation links

        img_tag = link.find("img")
        cover = img_tag.get("src", "").strip() if img_tag else ""

        # Title: prefer img alt text, then first non-trivial text node
        title = (img_tag.get("alt", "").strip() if img_tag else "") or ""
         # If it ends with "cover", strip it out
        if title.endswith("cover"):
            title = title[:-5].strip()  # Cut off 'title' and clean up any remaining spaces

        if not title:
            for el in link.descendants:
                t = el.get_text(strip=True) if hasattr(el, "get_text") else ""
                if t and len(t) > 2:
                    title = t
                    break

        if not title:
            continue  # skip entries we can't name

        seen.add(series_id)
        out.append({
            "id":    series_id,
            "title": title,
            "cover": cover,
            "url":   f"https://weebcentral.com/series/{series_id}",
        })
        if len(out) >= limit:
            break

    return out


@app.get("/api/search/weebcentral", dependencies=[Depends(_scrape_rl)])
async def search_weebcentral(q: str = ""):
    """
    Proxy WeebCentral search (GET /search/data) → JSON list of manga series.
    We proxy it server-side to avoid CORS. Results are cached per query for a
    couple of minutes so repeated searches don't hammer WeebCentral.

    Returns: { results: [{ id, title, cover, url }] }
    """
    q_norm = q.strip()
    if not q_norm:
        return {"results": []}
    return await _cache.get_or_set(
        f"wc:search:{q_norm.lower()}", 120, lambda: _search_weebcentral_fetch(q_norm)
    )


async def _search_weebcentral_fetch(q: str) -> dict:
    """Uncached WeebCentral search fetch — wrapped by search_weebcentral()."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            # Build params — excluded_tag works only for single-word capitalised
            # tag names; multi-word tags (e.g. "Reverse Harem") cause WC to
            # redirect to their /400 page regardless of encoding.
            # "Harem" covers both Harem and Reverse-Harem series in practice.
            wc_params: list[tuple[str, str]] = [
                ("text",         q),
                ("sort",         "Best Match"),
                ("order",        "Descending"),
                ("official",     "Any"),
                ("anime",        "Any"),
                ("adult",        "Any"),
                ("display_mode", "Full Display"),
                ("author",       ""),
                ("excluded_tag", "Harem"),
                ("excluded_tag", "Hentai"),
            ]
            res = await client.get(
                "https://weebcentral.com/search/data",
                params=wc_params,
                headers={**_WC_HEADERS, "HX-Request": "true"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeebCentral unreachable: {exc}")

    if not res.is_success:
        raise HTTPException(status_code=502, detail="WeebCentral search returned an error.")

    return {"results": _parse_wc_series(res.text)}


@app.get("/api/weebcentral/featured", dependencies=[Depends(_scrape_rl)])
async def weebcentral_featured():
    """
    Return the latest-updated series from WeebCentral for the discover page.
    Uses /search/data with adult=False so explicit content is excluded.

    Ecchi/Mature-tagged series are excluded from these "suggested" results
    (home page "Trending Now" row + discover page default grid) but will
    still show up if the user searches for them directly via
    /api/search/weebcentral.

    To surface more Adventure content, we additionally fetch a second pool
    filtered to included_tag=Adventure and interleave it with the general
    "Latest Updates" pool (deduplicated, capped at 24 total).

    The merged result is cached for a few minutes (it's the same for everyone)
    so the home/discover page doesn't re-scrape WeebCentral on every visit.

    Returns: { results: [{ id, title, cover, url }] }
    """
    return await _cache.get_or_set("wc:featured", 300, _weebcentral_featured_fetch)


async def _weebcentral_featured_fetch() -> dict:
    """Uncached featured fetch — wrapped by weebcentral_featured()."""
    import httpx

    _EXCLUDED_TAGS = ["Harem", "Hentai", "Ecchi", "Mature", "Romance"]

    def _wc_params(included_tag: str | None = None) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("text",         ""),
            ("sort",         "Latest Updates"),
            ("order",        "Descending"),
            ("official",     "Any"),
            ("anime",        "Any"),
            ("adult",        "Any"),
            ("display_mode", "Full Display"),
            ("author",       ""),
        ]
        if included_tag:
            params.append(("included_tag", included_tag))
        params.extend(("excluded_tag", t) for t in _EXCLUDED_TAGS)
        return params

    async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
        try:
            general_res, adventure_res = await asyncio.gather(
                client.get(
                    "https://weebcentral.com/search/data",
                    params=_wc_params(),
                    headers={**_WC_HEADERS, "HX-Request": "true"},
                ),
                client.get(
                    "https://weebcentral.com/search/data",
                    params=_wc_params("Adventure"),
                    headers={**_WC_HEADERS, "HX-Request": "true"},
                ),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeebCentral unreachable: {exc}")

    if not general_res.is_success:
        raise HTTPException(status_code=502, detail="WeebCentral featured unavailable.")

    general   = _parse_wc_series(general_res.text, limit=24)
    adventure = _parse_wc_series(adventure_res.text, limit=24) if adventure_res.is_success else []

    # Interleave adventure[0], general[0], adventure[1], general[1], … so
    # Adventure-tagged series are boosted without losing variety. Dedupe by
    # series id and cap at 24.
    merged: list[dict] = []
    seen: set[str] = set()
    ai = gi = 0
    while len(merged) < 24 and (ai < len(adventure) or gi < len(general)):
        if ai < len(adventure):
            series = adventure[ai]
            ai += 1
            if series["id"] not in seen:
                seen.add(series["id"])
                merged.append(series)
        if len(merged) >= 24:
            break
        if gi < len(general):
            series = general[gi]
            gi += 1
            if series["id"] not in seen:
                seen.add(series["id"])
                merged.append(series)

    return {"results": merged}


@app.get("/api/weebcentral/series/{series_id}", dependencies=[Depends(_scrape_rl)])
async def weebcentral_series_info(series_id: str):
    """
    Return title, cover and description for a WeebCentral series by scraping its
    page. Cached per series for ~10 min (series metadata changes rarely).
    Returns: { id, title, cover, description, url }
    """
    return await _cache.get_or_set(
        f"wc:series:{series_id}", 600, lambda: _weebcentral_series_info_fetch(series_id)
    )


async def _weebcentral_series_info_fetch(series_id: str) -> dict:
    """Uncached series-info scrape — wrapped by weebcentral_series_info()."""
    import httpx
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            res = await client.get(
                f"https://weebcentral.com/series/{series_id}",
                headers=_WC_HEADERS,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    if not res.is_success:
        raise HTTPException(status_code=404, detail="Series not found on WeebCentral.")

    soup = BeautifulSoup(res.text, "html.parser")

    # Title — <h1> or <title> tag
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else series_id
    # Clean up " - WeebCentral" suffix from <title>
    for suffix in [" - WeebCentral", " | WeebCentral"]:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()

    # Cover — og:image meta or first series img
    og_img = soup.find("meta", property="og:image")
    cover  = og_img.get("content", "").strip() if og_img else ""
    if not cover:
        img = soup.find("img", alt=True)
        cover = img.get("src", "").strip() if img else ""

    # Description — WeebCentral puts it inside a <li> whose first child is
    # <strong>Description</strong>, not in og:description (which is usually
    # a generic site blurb).  Walk all <strong> elements to find it.
    description = ""
    for strong in soup.find_all("strong"):
        if strong.get_text(strip=True).lower() == "description":
            # The <p> sibling that holds the actual text
            p = strong.find_next_sibling("p")
            if p:
                description = p.get_text(separator="\n", strip=True)
                break
    # Fallback to og:description if the section wasn't found
    if not description:
        og_desc = (soup.find("meta", property="og:description")
                   or soup.find("meta", attrs={"name": "description"}))
        description = og_desc.get("content", "").strip() if og_desc else ""

    # Tags — look for <strong>Tag(s)</strong> and collect the <a> links nearby.
    # WC links look like: href="https://weebcentral.com/search?included_tag=Action"
    tags: list[str] = []
    for strong in soup.find_all("strong"):
        txt = strong.get_text(strip=True).lower()
        if "tag" in txt:
            parent = strong.parent  # usually a <li> or <div>
            container = parent if parent else strong
            for link in container.find_all("a", href=True):
                href = link.get("href", "")
                if "included_tag" in href or "tag" in href.lower():
                    tag_name = link.get_text(strip=True)
                    if tag_name and tag_name not in tags:
                        tags.append(tag_name)
            break

    return {
        "id":          series_id,
        "title":       title,
        "cover":       cover,
        "description": description,
        "tags":        tags,
        "url":         f"https://weebcentral.com/series/{series_id}",
    }


_WC_DATE_RE = _re.compile(
    r'\d{4}-\d{2}-\d{2}|T\d{2}:\d{2}|last\s*read',
    _re.IGNORECASE,
)
# ULID: exactly 26 chars from the Crockford Base32 alphabet
_WC_ULID_RE = _re.compile(r'\b[0-9A-HJKMNP-TV-Z]{26}\b', _re.IGNORECASE)


def _clean_chapter_label(link_tag) -> str:
    """
    Extract a clean chapter title from a WeebCentral chapter <a> element.

    WeebCentral embeds "Last Read" labels, ISO timestamps, and raw ULID strings
    as child elements inside the link.  We strip all of those so the result
    contains only the human-readable part, e.g. "Chapter 12".
    """
    parts: list[str] = []
    for child in link_tag.children:
        text = (child.get_text(strip=True) if hasattr(child, "get_text")
                else str(child).strip())
        if not text:
            continue
        if _WC_DATE_RE.search(text):
            continue
        # Remove any embedded ULID tokens (e.g. "Chapter 01JCH988AN8EMQD6E3S81NEPX5")
        text = _WC_ULID_RE.sub("", text).strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


@app.get("/api/weebcentral/series/{series_id}/chapters", dependencies=[Depends(_scrape_rl)])
async def weebcentral_series_chapters(series_id: str):
    """
    Return the FULL chapter list for a WeebCentral series using the
    /full-chapter-list endpoint (shows all chapters without needing "show all").
    Cached per series for ~10 min so the series page doesn't re-scrape on every
    load / filter toggle.

    Returns: { chapters: [{ id, number, title, url }] }
    Chapters are returned newest-first (WeebCentral's natural order).
    """
    return await _cache.get_or_set(
        f"wc:chapters:{series_id}", 600, lambda: _weebcentral_series_chapters_fetch(series_id)
    )


async def _weebcentral_series_chapters_fetch(series_id: str) -> dict:
    """Uncached chapter-list scrape — wrapped by weebcentral_series_chapters()."""
    import httpx
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        try:
            res = await client.get(
                f"https://weebcentral.com/series/{series_id}/full-chapter-list",
                headers={**_WC_HEADERS, "HX-Request": "true"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeebCentral unreachable: {exc}")

    if not res.is_success:
        raise HTTPException(status_code=404, detail="Chapter list not found on WeebCentral.")

    soup = BeautifulSoup(res.text, "html.parser")
    chapters: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        href: str = link.get("href", "")
        if "/chapters/" not in href:
            continue

        ch_id = href.rstrip("/").split("/chapters/")[-1].split("/")[0]
        if not ch_id or ch_id in seen:
            continue
        if not _WC_SERIES_ID_RE.match(ch_id):
            continue
        seen.add(ch_id)

        # WeebCentral full-chapter-list structure inside each <a>:
        #   <span class="me-2"><!-- checkmark SVG --></span>
        #   <span class="grow flex items-center gap-2">
        #     <span class="">Chapter 386</span>     ← title we want
        #     <span x-show="last_read_...">...</span>   ← hidden "Last Read" badge
        #     <span x-show="new_chapter">...</span>     ← hidden "NEW" badge
        #   </span>
        #   <time>Nov 13, 2024</time>
        #
        # We navigate directly to the first child <span> of the "grow" span
        # to avoid touching the hidden badge text at all.
        label = ""
        grow_span = link.find("span", class_="grow")
        if grow_span:
            for child in grow_span.children:
                if getattr(child, "name", None) == "span":
                    text = child.get_text(strip=True)
                    if text:
                        label = text
                        break

        if not label:
            continue  # skip entries we can't name cleanly

        # Extract bare chapter number: "Chapter 386" → "386"
        num_match = _re.search(r'(?:chapter|ch\.?)\s*([\d.]+)', label, _re.I)
        number = num_match.group(1) if num_match else ""

        chapters.append({
            "id":     ch_id,
            "number": number,
            "title":  label,
            "url":    f"https://weebcentral.com/chapters/{ch_id}",
        })

    return {"chapters": chapters}


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------

@app.get("/api/library", dependencies=[Depends(_api_rl)])
async def get_library():
    """
    Return all completed chapters in the shared library, newest first.
    Chapters are grouped by manga_id on the frontend.
    """
    chapters = await library.list_chapters()
    return {"chapters": chapters, "library_enabled": library.library_enabled()}


@app.post("/api/library/rescan", status_code=200)
async def rescan_library():
    """
    Two-phase library recovery scan:

    Phase 1 — local jobs:
      Walk data/jobs/ and register any completed job that either isn't in the
      library DB yet, OR is registered with a local /api/ URL while R2 is now
      configured (so the entry gets upgraded to a permanent R2 URL).

    Phase 2 — R2 discovery (only when R2 is configured):
      List all chapter folders in Cloudflare R2, fetch metadata from the
      MangaDex API (or WeebCentral for wc: IDs), and register any chapter
      that is in R2 but not in the local job directory (e.g. the job was
      already cleaned up or translated on a different machine).

    Returns: { registered, updated, skipped, failed }
    """
    r2_mode = library._r2_mode()
    registered = updated = skipped = failed = 0

    # ── Phase 1: local jobs ───────────────────────────────────────────────────
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        meta_path     = job_dir / "chapter_meta.json"
        output_dir    = job_dir / "output"
        pdf_path      = output_dir / "result_compressed.pdf"
        pdf_path_full = output_dir / "result.pdf"
        if not meta_path.exists() or (not pdf_path.exists() and not pdf_path_full.exists()):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            mangadex_id = meta.get("mangadex_id", "")
            if not mangadex_id:
                continue

            cached = await library.check_cache(mangadex_id)
            if cached:
                # If R2 is now configured but the stored URL is still a local
                # /api/ path, force-reregister to upgrade to R2 URLs.
                if r2_mode and str(cached.get("pdf_url", "")).startswith("/api/"):
                    await _register_in_library(job_dir)
                    updated += 1
                else:
                    skipped += 1
                continue

            await _register_in_library(job_dir)
            registered += 1
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "[rescan] Local job %s failed: %s", job_dir.name, exc
            )
            failed += 1

    # ── Phase 2: R2 discovery ─────────────────────────────────────────────────
    if r2_mode:
        try:
            r2_results = await _scan_r2_chapters()
            registered += r2_results["registered"]
            skipped    += r2_results["skipped"]
            failed     += r2_results["failed"]
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).error("[rescan] R2 scan crashed: %s", exc, exc_info=True)
            failed += 1

    return {"registered": registered, "updated": updated,
            "skipped": skipped, "failed": failed}


async def _scan_r2_chapters() -> dict:
    """
    List all chapter folders in Cloudflare R2 and register any that are not
    already in the library DB.

    R2 layout:  chapters/{mangadex_id}/compressed.pdf
                chapters/{mangadex_id}/pages/001.jpg …

    Metadata is fetched from MangaDex API (UUID) or WeebCentral scrape (wc:…).
    """
    import logging as _log
    import httpx

    _logger = _log.getLogger(__name__)
    registered = skipped = failed = 0

    try:
        import aioboto3  # noqa: PLC0415
    except ImportError:
        _logger.warning("[r2-scan] aioboto3 not installed; skipping R2 discovery")
        return {"registered": 0, "skipped": 0, "failed": 0}

    def _r2_env(k: str) -> str:
        import os
        return os.getenv(k, "").strip()

    r2_public = _r2_env("R2_PUBLIC_URL").rstrip("/")
    bucket    = _r2_env("R2_BUCKET")
    endpoint  = f"https://{_r2_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_r2_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_r2_env("R2_SECRET_KEY"),
        region_name="auto",
    ) as s3:
        # List all "folders" directly under chapters/ (no paginator — few chapters)
        resp = await s3.list_objects_v2(
            Bucket=bucket, Prefix="chapters/", Delimiter="/"
        )
        common_prefixes = resp.get("CommonPrefixes", [])
        _logger.info("[r2-scan] Found %d chapter folder(s) in R2", len(common_prefixes))

        for cp in common_prefixes:
            folder = cp["Prefix"]           # e.g. "chapters/afaebc64-.../
            parts  = folder.rstrip("/").split("/")
            if len(parts) < 2:
                continue
            mangadex_id = parts[1]

            cached = await library.check_cache(mangadex_id)
            if cached:
                skipped += 1
                continue

            pdf_key   = f"{folder}compressed.pdf"
            pages_pfx = f"{folder}pages"

            # Verify PDF exists and get size
            try:
                head = await s3.head_object(Bucket=bucket, Key=pdf_key)
                pdf_size_kb = head["ContentLength"] // 1024
            except Exception:
                _logger.debug("[r2-scan] No PDF at %s — skip", pdf_key)
                continue

            # Count page images
            pages_resp = await s3.list_objects_v2(
                Bucket=bucket, Prefix=f"{pages_pfx}/"
            )
            page_count = len(pages_resp.get("Contents", []))

            # Fetch chapter metadata from MangaDex / WeebCentral
            try:
                async with httpx.AsyncClient(timeout=10.0) as hclient:
                    meta = await _fetch_chapter_meta_for_id(hclient, mangadex_id)
            except Exception as exc:
                _logger.warning("[r2-scan] Metadata fetch failed for %s: %s", mangadex_id, exc)
                meta = {}   # register with minimal data rather than skip

            lid = await library.register_chapter(
                mangadex_id   = mangadex_id,
                manga_title   = meta.get("manga_title", "Unknown"),
                manga_id      = meta.get("manga_id", ""),
                chapter_num   = meta.get("chapter_num", ""),
                chapter_title = meta.get("chapter_title", ""),
                cover_url     = meta.get("cover_url", ""),
                page_count    = page_count,
                pdf_url       = f"{r2_public}/{pdf_key}",
                pages_prefix  = f"{r2_public}/{pages_pfx}",
                pdf_size_kb   = pdf_size_kb,
            )
            if lid:
                _logger.info("[r2-scan] Registered %s → %s", mangadex_id, lid)
                registered += 1
            else:
                failed += 1

    return {"registered": registered, "skipped": skipped, "failed": failed}


async def _fetch_chapter_meta_for_id(client, mangadex_id: str) -> dict:
    """
    Return { manga_title, manga_id, chapter_num, chapter_title, cover_url }
    for a chapter ID that may be a MangaDex UUID or a WeebCentral wc:ULID.
    """
    import re

    _MD_API = "https://api.mangadex.org"
    _WC_BASE = "https://weebcentral.com"
    _WC_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}", re.IGNORECASE)

    # ── WeebCentral ────────────────────────────────────────────────────────────
    if mangadex_id.startswith("wc:"):
        wc_id = mangadex_id[3:]
        wc_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": _WC_BASE + "/",
        }
        r = await client.get(f"{_WC_BASE}/chapters/{wc_id}", headers=wc_headers)
        if not r.is_success:
            return {}
        from bs4 import BeautifulSoup  # noqa: PLC0415
        soup = BeautifulSoup(r.text, "html.parser")
        series_id = series_title = chapter_num = cover_url = ""
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "/series/" in href:
                m = _WC_ID_RE.search(href)
                if m:
                    series_id    = m.group(0).upper()
                    series_title = link.get_text(strip=True)
                    break
        title_tag = soup.find("title")
        if title_tag:
            nm = re.search(r"chapter\s*([\d.]+)", title_tag.get_text(), re.I)
            chapter_num = nm.group(1) if nm else ""
        og = soup.find("meta", property="og:image")
        if og:
            cover_url = og.get("content", "").strip()
        return {
            "manga_title":   series_title or "Unknown",
            "manga_id":      series_id,
            "chapter_num":   chapter_num,
            "chapter_title": "",
            "cover_url":     cover_url,
        }

    # ── MangaDex ───────────────────────────────────────────────────────────────
    manga_id = manga_title = chapter_title = cover_url = ""
    chapter_num = "?"
    try:
        r = await client.get(
            f"{_MD_API}/chapter/{mangadex_id}",
            params={"includes[]": ["manga"]},
            headers={"User-Agent": "HebrewMangaTranslator/0.1"},
        )
        r.raise_for_status()
        data  = r.json().get("data", {})
        attrs = data.get("attributes", {})
        chapter_num   = attrs.get("chapter") or "?"
        chapter_title = attrs.get("title") or ""
        for rel in data.get("relationships", []):
            if rel.get("type") == "manga":
                manga_id = rel.get("id", "")
                titles   = (rel.get("attributes") or {}).get("title", {})
                manga_title = (
                    titles.get("en") or titles.get("ja-ro")
                    or next(iter(titles.values()), "")
                )
                break
    except Exception:
        pass

    if manga_id:
        try:
            cr = await client.get(
                f"{_MD_API}/cover",
                params={"manga[]": manga_id, "limit": 1, "order[volume]": "asc"},
                headers={"User-Agent": "HebrewMangaTranslator/0.1"},
            )
            cr.raise_for_status()
            covers = cr.json().get("data", [])
            if covers:
                fname = covers[0]["attributes"]["fileName"]
                cover_url = (
                    f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
                )
        except Exception:
            pass

    return {
        "manga_title":   manga_title or "Unknown",
        "manga_id":      manga_id,
        "chapter_num":   chapter_num,
        "chapter_title": chapter_title,
        "cover_url":     cover_url,
    }


@app.get("/api/library/manga/{mangadex_manga_id}", dependencies=[Depends(_api_rl)])
async def get_library_by_manga(mangadex_manga_id: str):
    """
    Return all translated chapters for a specific manga UUID.
    Works in both local (SQLite) and cloud (Supabase) modes.

    Returns: { chapters: [{id, mangadex_id, chapter_num, chapter_title}] }
    """
    chapters = await library.list_chapters_by_manga(mangadex_manga_id)
    return {"chapters": chapters}


@app.get("/api/library/{chapter_id}", dependencies=[Depends(_api_rl)])
async def get_library_chapter(chapter_id: str):
    """
    Return a single chapter's metadata (including pdf_url and pages_prefix).
    Used by the web reader page.
    """
    chapter = await library.get_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found in library.")
    return chapter


@app.get("/api/library/local-pages/{job_id}/{filename}")
async def serve_local_page(job_id: str, filename: str):
    """
    Serve a single JPEG page from a locally stored translation job.

    Used by the web reader in local mode (no Cloudflare R2 configured).
    pages_prefix is set to /api/library/local-pages/{job_id} by the local
    library backend; the reader appends /001.jpg, /002.jpg, … to load pages.
    """
    if not filename.endswith(".jpg"):
        raise HTTPException(status_code=400, detail="Only .jpg files are served here.")
    page_file = JOBS_DIR / job_id / "output" / "pages" / filename
    if not page_file.exists():
        raise HTTPException(status_code=404, detail="Page not found.")
    return FileResponse(page_file, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def _load_existing_pages(job_dir: Path) -> list[Path]:
    """
    Reconstruct the pages list from whatever PNGs are already in original/.
    Returns them sorted by filename (001.png, 002.png, …).
    """
    return sorted(
        p for p in (job_dir / "original").glob("*.png")
        if p.stem.isdigit()
    )


async def _run_pipeline_from_step(
    job_id:    str,
    job_dir:   Path,
    pages:     list[Path],
    from_step: str,
) -> None:
    """
    Run the pipeline starting at `from_step`, reusing earlier artifacts on disk.

    Step order: detect → ocr → inpaint → translate → typeset
    """
    emit = job_manager.get_emitter(job_id)
    _steps = ["detect", "ocr", "inpaint", "translate", "typeset"]
    start  = _steps.index(from_step)

    # Recover the per-job Modal tokens so detect/inpaint run on the user's GPU
    # (same BYOK policy as a fresh job — there is no local-GPU fallback).
    cfg_path = job_dir / "job_config.json"
    job_cfg  = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    modal_token_id:     str | None = job_cfg.get("modal_token_id")     or None
    modal_token_secret: str | None = job_cfg.get("modal_token_secret") or None

    try:
        # Gate heavy work behind the global concurrency cap (see _run_pipeline).
        async with _pipeline_sem:
            if start <= 0:
                pages = await detector.detect(
                    job_dir, pages, emit, modal_token_id, modal_token_secret
                )
            if start <= 1:
                pages = await ocr.ocr(job_dir, pages, emit)
            if start <= 2:
                pages = await inpainter.inpaint(
                    job_dir, pages, emit, modal_token_id, modal_token_secret
                )
            if start <= 3:
                pages = await translator.translate(job_dir, pages, emit)
            if start <= 4:
                pages = await typesetter.typeset(job_dir, pages, emit)

            await emit({
                "stage":        "done",
                "total_pages":  len(pages),
                "download_url": f"/api/jobs/{job_id}/download",
            })

            # Clean up intermediate artifacts (register in library below).
            asyncio.create_task(_cleanup_intermediates(job_dir))

        # Register outside the semaphore — same as the URL pipeline so resumed
        # jobs are also indexed without holding a concurrency slot during upload.
        await _register_in_library(job_dir, emit=emit)

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
        # Delete the failed job directory so it doesn't accumulate on disk.
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass
    finally:
        _release_ip_slot(job_id)
