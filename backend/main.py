from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
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
from core import library

app = FastAPI(title="Hebrew Manga Translator API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS_DIR = Path("data/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = 200
ALLOWED_EXTENSIONS = {".pdf", ".zip"}

job_manager = JobManager()


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class FetchChapterBody(BaseModel):
    url:        str
    data_saver: bool = False


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile = File(...)):
    """Accept a .pdf or .zip upload, spin up the translation pipeline."""
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

    upload_path = job_dir / f"source{suffix}"
    upload_path.write_bytes(content)

    job_manager.register_job(job_id)
    asyncio.create_task(_run_pipeline(job_id, upload_path))

    return {"job_id": job_id}


@app.post("/api/jobs/from-url", status_code=201)
async def create_job_from_url(body: FetchChapterBody):
    """
    Download a MangaDex chapter by URL or UUID, then run the translation pipeline.

    Body JSON:
      { "url": "https://mangadex.org/chapter/<uuid>", "data_saver": false }

    data_saver: set true for lower-resolution images (faster download, smaller file).

    If the chapter has already been translated (library cache hit), returns:
      { "job_id": null, "cached": true, "library_id": "<uuid>" }
    and no pipeline is started.  The frontend should redirect to /library/<library_id>.
    """
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
            # Generate compressed PDF in a thread (CPU-bound image work)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _build_compressed_pdf, output_dir, result)
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
async def resume_job(job_id: str, body: ResumeBody):
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
# Shared pipeline steps (Steps 1-5 + done emit)
# ---------------------------------------------------------------------------

async def _run_pipeline_steps(
    job_id:  str,
    job_dir: Path,
    pages:   list[Path],
    emit,
) -> None:
    """
    Run Steps 1-5 of the pipeline and emit the final 'done' event.

    Called by both _run_pipeline (file upload) and _run_pipeline_from_url
    (MangaDex download) after pages are already in original/.
    """
    # ── Step 1: Detect ─────────────────────────────────────────────────────
    pages = await detector.detect(job_dir, pages, emit)

    # ── Step 2: OCR ────────────────────────────────────────────────────────
    pages = await ocr.ocr(job_dir, pages, emit)

    # ── Step 3: Inpaint ────────────────────────────────────────────────────
    pages = await inpainter.inpaint(job_dir, pages, emit)

    # ── Step 4: Translate ──────────────────────────────────────────────────
    pages = await translator.translate(job_dir, pages, emit)

    # ── Step 5: Typeset ────────────────────────────────────────────────────
    pages = await typesetter.typeset(job_dir, pages, emit)

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
        # ── Step 0: Split ──────────────────────────────────────────────────
        pages = await splitter.split(job_dir, source_file, emit)

        await _run_pipeline_steps(job_id, job_dir, pages, emit)

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})


async def _run_pipeline_from_url(
    job_id:     str,
    job_dir:    Path,
    url:        str,
    data_saver: bool,
) -> None:
    """Pipeline runner for MangaDex URL jobs."""
    emit = job_manager.get_emitter(job_id)

    try:
        # ── Step 0: Download ───────────────────────────────────────────────
        pages = await download_chapter(url, job_dir, emit, data_saver=data_saver)

        await _run_pipeline_steps(job_id, job_dir, pages, emit)

        # ── Library: upload to R2 + register in Supabase ──────────────────
        # Runs after the "done" event is emitted so the user already has their
        # download link; library registration is best-effort (errors are logged
        # but never surface to the user).
        asyncio.create_task(_register_in_library(job_dir))

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

async def _register_in_library(job_dir: Path) -> None:
    """
    Upload output files to R2 and register the chapter in Supabase.

    Silently skipped when library is disabled.  Errors are logged but never
    re-raised — the user already received their download link.
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

        await library.register_chapter(
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

    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[library] _register_in_library failed: %s", exc)


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------

@app.get("/api/library")
async def get_library():
    """
    Return all completed chapters in the shared library, newest first.
    Chapters are grouped by manga_id on the frontend.
    """
    chapters = await library.list_chapters()
    return {"chapters": chapters, "library_enabled": library.library_enabled()}


@app.get("/api/library/{chapter_id}")
async def get_library_chapter(chapter_id: str):
    """
    Return a single chapter's metadata (including pdf_url and pages_prefix).
    Used by the web reader page.
    """
    chapter = await library.get_chapter(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found in library.")
    return chapter


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

    try:
        if start <= 0:
            pages = await detector.detect(job_dir, pages, emit)
        if start <= 1:
            pages = await ocr.ocr(job_dir, pages, emit)
        if start <= 2:
            pages = await inpainter.inpaint(job_dir, pages, emit)
        if start <= 3:
            pages = await translator.translate(job_dir, pages, emit)
        if start <= 4:
            pages = await typesetter.typeset(job_dir, pages, emit)

        await emit({
            "stage":        "done",
            "total_pages":  len(pages),
            "download_url": f"/api/jobs/{job_id}/download",
        })

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
