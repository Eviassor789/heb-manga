from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
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
    allow_origins=["*"],          # tightened per-origin in production via env var if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],          # includes X-Gemini-Api-Key, X-Modal-Token-Id/Secret
)

JOBS_DIR = Path("data/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = 200
ALLOWED_EXTENSIONS = {".pdf", ".zip"}

job_manager = JobManager()


# ---------------------------------------------------------------------------
# Startup: scan completed jobs and register any that are not yet in the library
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup_scan_library() -> None:
    """
    On every server start, walk data/jobs/ and register any job that:
      • has a chapter_meta.json  (was a URL-based job with metadata)
      • has output/result_compressed.pdf  (pipeline ran to completion)
      • is NOT already in the library DB (checked by mangadex_id)

    This makes library registration resilient to server restarts that killed
    the background task before it could commit.
    """
    registered = skipped = failed = 0
    r2_mode = library._r2_mode()
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        meta_path = job_dir / "chapter_meta.json"
        # Accept either result.pdf or result_compressed.pdf as completion markers
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

    if registered or failed:
        import logging as _log
        _log.getLogger(__name__).info(
            "[startup] Library scan: %d registered, %d skipped (already in DB), %d failed",
            registered, skipped, failed,
        )


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


def _save_job_config(job_dir: Path, request: Request) -> None:
    """
    Persist per-job settings supplied by the client as request headers.

    Stored fields:
      gemini_api_key  — X-Gemini-Api-Key (user's Gemini key for OCR + translation)

    The key lives only in job_config.json inside the job directory and is removed
    when the job is deleted.  It is NEVER stored in any database.
    GPU processing (Modal) and storage (R2/Supabase) always use the server's own keys.
    """
    config: dict = {}

    gemini_key = request.headers.get("X-Gemini-Api-Key", "").strip()
    if gemini_key:
        config["gemini_api_key"] = gemini_key

    (job_dir / "job_config.json").write_text(
        json.dumps(config, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/jobs", status_code=201)
async def create_job(request: Request, file: UploadFile = File(...)):
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
    _save_job_config(job_dir, request)

    upload_path = job_dir / f"source{suffix}"
    upload_path.write_bytes(content)

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
    _save_job_config(job_dir, request)

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

        # ── Cleanup: remove intermediate dirs to save disk space ──────────
        asyncio.create_task(_cleanup_intermediates(job_dir))

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

        # ── Cleanup: remove intermediate dirs to save disk space ──────────
        asyncio.create_task(_cleanup_intermediates(job_dir))

        # ── Library: build compressed PDF + register chapter ──────────────
        # "done" was already emitted inside _run_pipeline_steps, so awaiting
        # registration here doesn't block the user from getting their link.
        # We await (rather than create_task) so the write commits before the
        # coroutine returns — a server restart no longer loses the entry.
        await _register_in_library(job_dir, emit=emit)

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})


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


@app.get("/api/search/weebcentral")
async def search_weebcentral(q: str = ""):
    """
    Proxy WeebCentral search (GET /search/data) → JSON list of manga series.
    We proxy it server-side to avoid CORS.

    Returns: { results: [{ id, title, cover, url }] }
    """
    if not q.strip():
        return {"results": []}

    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            res = await client.get(
                "https://weebcentral.com/search/data",
                params={
                    "text":         q,
                    "sort":         "Best Match",
                    "order":        "Descending",
                    "official":     "Any",
                    "anime":        "Any",
                    "adult":        "Any",
                    "display_mode": "Full Display",
                    "author":       "",
                },
                headers={**_WC_HEADERS, "HX-Request": "true"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeebCentral unreachable: {exc}")

    if not res.is_success:
        raise HTTPException(status_code=502, detail="WeebCentral search returned an error.")

    return {"results": _parse_wc_series(res.text)}


@app.get("/api/weebcentral/featured")
async def weebcentral_featured():
    """
    Scrape WeebCentral's main page and return the first ~24 series from the
    Hot Updates section so the discover page has something to show on load.

    Returns: { results: [{ id, title, cover, url }] }
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
        try:
            res = await client.get("https://weebcentral.com/", headers=_WC_HEADERS)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeebCentral unreachable: {exc}")

    if not res.is_success:
        raise HTTPException(status_code=502, detail="WeebCentral main page unavailable.")

    return {"results": _parse_wc_series(res.text, limit=24)}


@app.get("/api/weebcentral/series/{series_id}")
async def weebcentral_series_info(series_id: str):
    """
    Return title, cover and description for a WeebCentral series by scraping its page.
    Returns: { id, title, cover, description, url }
    """
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


@app.get("/api/weebcentral/series/{series_id}/chapters")
async def weebcentral_series_chapters(series_id: str):
    """
    Return the FULL chapter list for a WeebCentral series using the
    /full-chapter-list endpoint (shows all chapters without needing "show all").

    Returns: { chapters: [{ id, number, title, url }] }
    Chapters are returned newest-first (WeebCentral's natural order).
    """
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

@app.get("/api/library")
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


@app.get("/api/library/manga/{mangadex_manga_id}")
async def get_library_by_manga(mangadex_manga_id: str):
    """
    Return all translated chapters for a specific manga UUID.
    Works in both local (SQLite) and cloud (Supabase) modes.

    Returns: { chapters: [{id, mangadex_id, chapter_num, chapter_title}] }
    """
    chapters = await library.list_chapters_by_manga(mangadex_manga_id)
    return {"chapters": chapters}


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

        # Clean up intermediate artifacts and register in library —
        # same as the full URL pipeline so resumed jobs are also indexed.
        asyncio.create_task(_cleanup_intermediates(job_dir))
        await _register_in_library(job_dir, emit=emit)

    except Exception as exc:
        await emit({"stage": "error", "message": str(exc)})
