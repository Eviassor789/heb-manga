from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Load backend/.env before any module reads os.getenv()
load_dotenv()

from pipeline import detector, inpainter, ocr, splitter, translator, typesetter
from utils.job_manager import JobManager
from utils.manga_downloader import download_chapter

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
    """
    job_id  = str(uuid.uuid4())
    job_dir = _create_job_dirs(job_id)

    job_manager.register_job(job_id)
    asyncio.create_task(
        _run_pipeline_from_url(job_id, job_dir, body.url, body.data_saver)
    )

    return {"job_id": job_id}


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
async def download_result(job_id: str):
    """Stream the finished translated PDF."""
    job_dir = _require_job(job_id)
    result = job_dir / "output" / "result.pdf"
    if not result.exists():
        raise HTTPException(status_code=404, detail="Result not ready yet.")
    return FileResponse(result, media_type="application/pdf", filename="translated_manga.pdf")


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Remove all job artifacts from disk."""
    job_dir = _require_job(job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    job_manager.remove_job(job_id)


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

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
