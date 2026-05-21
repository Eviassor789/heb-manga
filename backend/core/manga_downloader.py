"""
MangaDex Chapter Downloader

Accepts a MangaDex chapter URL (or bare UUID) and downloads all pages as
numbered PNGs into <job_dir>/original/, ready to feed into the translation
pipeline.

Supported URL forms
───────────────────
  https://mangadex.org/chapter/{uuid}
  https://mangadex.org/chapter/{uuid}/{page-number}
  {uuid}   ← bare UUID (36-char hex-hyphen string)

MangaDex public API — no authentication required for reading
  Chapter metadata : GET https://api.mangadex.org/chapter/{id}
  CDN server info  : GET https://api.mangadex.org/at-home/server/{id}
  Page image       : GET {baseUrl}/data/{hash}/{filename}          (HQ)
                   : GET {baseUrl}/data-saver/{hash}/{filename}    (LQ)

Rate-limit policy
─────────────────
MangaDex guidelines request ≥ 40 ms between CDN requests when using the
at-home network.  We use 0.35 s (generous) to stay well within limits.

Reference: https://api.mangadex.org/docs/
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from core.job_manager import EmitFn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MD_API      = "https://api.mangadex.org"
_UUID_RE     = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_INTER_PAGE_DELAY = 0.35   # seconds between CDN image requests
_REQUEST_TIMEOUT  = 30.0   # seconds per HTTP request
_MAX_RETRIES      = 3      # per-image retry attempts

_HEADERS = {
    "User-Agent": "HebrewMangaTranslator/0.1 (contact: github.com/evias)",
}

# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def download_chapter(
    url_or_id: str,
    job_dir:   Path,
    emit:      EmitFn,
    *,
    data_saver: bool = False,
) -> list[Path]:  # noqa: D401
    """
    Download all pages of a MangaDex chapter to <job_dir>/original/.

    Parameters
    ----------
    url_or_id   : MangaDex chapter URL or bare chapter UUID.
    job_dir     : Root job directory (original/ subdir must already exist).
    emit        : SSE emitter from the job manager.
    data_saver  : If True, use lower-resolution 'data-saver' images (~40 %
                  smaller).  False = full resolution (default).

    Returns
    -------
    Sorted list of Path objects for every downloaded page image, suitable for
    passing directly to the next pipeline step.

    Raises
    ------
    ValueError   : URL/ID cannot be parsed or resolved to a chapter UUID.
    RuntimeError : API request failed or chapter has no pages.
    """
    chapter_id = _extract_uuid(url_or_id)
    await emit({"stage": "download", "status": "starting",
                "chapter_id": chapter_id})

    async with httpx.AsyncClient(
        headers=_HEADERS,
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        # ── Step A: Resolve chapter metadata + CDN server info in parallel ─
        (chapter_meta, (base_url, img_hash, filenames)) = await asyncio.gather(
            _fetch_chapter_metadata(client, chapter_id),
            _fetch_server_info(client, chapter_id, data_saver=data_saver),
        )
        chapter_label = chapter_meta["label"]

        total = len(filenames)
        if total == 0:
            raise RuntimeError(
                f"Chapter {chapter_id} has no pages in MangaDex API response."
            )

        log.info(
            "[downloader] '%s' — %d page(s), quality=%s",
            chapter_label, total, "data-saver" if data_saver else "full",
        )

        quality_dir = "data-saver" if data_saver else "data"
        out_dir     = job_dir / "original"
        out_dir.mkdir(parents=True, exist_ok=True)
        pages: list[Path] = []

        # ── Step C: Download images ───────────────────────────────────────
        for i, filename in enumerate(filenames, start=1):
            img_url  = f"{base_url}/{quality_dir}/{img_hash}/{filename}"
            out_path = out_dir / f"{i:03d}.png"

            await _download_image(client, img_url, out_path, attempt=1)

            pages.append(out_path)
            await emit({
                "stage":         "download",
                "status":        "running",
                "page":          i,
                "total":         total,
                "chapter_title": chapter_label,
            })

            if i < total:
                await asyncio.sleep(_INTER_PAGE_DELAY)

    # Persist title (legacy, used by resume endpoint)
    (job_dir / "chapter_title.txt").write_text(chapter_label, encoding="utf-8")

    # Persist full metadata for library registration after pipeline completes
    import json as _json  # noqa: PLC0415
    meta_path = job_dir / "chapter_meta.json"
    meta_path.write_text(
        _json.dumps({
            "mangadex_id":   chapter_id,
            "manga_id":      chapter_meta.get("manga_id", ""),
            "manga_title":   chapter_meta.get("manga_title", ""),
            "chapter_num":   chapter_meta.get("chapter_num", ""),
            "chapter_title": chapter_meta.get("chapter_title", ""),
            "cover_url":     chapter_meta.get("cover_url", ""),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    await emit({
        "stage":         "download",
        "status":        "done",
        "total_pages":   total,
        "chapter_title": chapter_label,
    })

    return sorted(pages)


# ---------------------------------------------------------------------------
# URL / UUID helpers
# ---------------------------------------------------------------------------

def _extract_uuid(url_or_id: str) -> str:
    """
    Extract a MangaDex chapter UUID from a URL or return it directly.

    Raises ValueError if no valid UUID can be found.
    """
    url_or_id = url_or_id.strip()
    match = _UUID_RE.search(url_or_id)
    if not match:
        raise ValueError(
            f"Cannot extract a MangaDex chapter UUID from: {url_or_id!r}\n"
            "  Expected forms:\n"
            "    https://mangadex.org/chapter/<uuid>\n"
            "    <bare-uuid>   e.g. a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
    return match.group(0).lower()


# Public alias — use this to extract the chapter ID before starting a download
# (needed by main.py for cache lookup prior to job creation)
extract_chapter_id = _extract_uuid


# ---------------------------------------------------------------------------
# MangaDex API calls
# ---------------------------------------------------------------------------

async def _fetch_chapter_metadata(client: httpx.AsyncClient, chapter_id: str) -> dict:
    """
    Fetch chapter + manga info from MangaDex API and fetch the manga cover art.

    Returns a dict with keys:
      label          Human-readable "Manga Title Ch. N — Episode Title"
      mangadex_id    Chapter UUID
      manga_id       Manga UUID (empty string on error)
      manga_title    English manga title (empty string on error)
      chapter_num    Chapter number string, e.g. "12" or "12.5"
      chapter_title  Episode title (may be empty)
      cover_url      MangaDex CDN cover URL (512 px, empty string on error)

    All fields degrade gracefully — errors fall back to empty strings so the
    pipeline never aborts due to metadata issues.
    """
    manga_id      = ""
    manga_title   = ""
    chapter_num   = "?"
    chapter_title = ""
    cover_url     = ""

    try:
        resp = await client.get(
            f"{_MD_API}/chapter/{chapter_id}",
            params={"includes[]": ["manga"]},
        )
        resp.raise_for_status()
        data  = resp.json().get("data", {})
        attrs = data.get("attributes", {})

        chapter_num   = attrs.get("chapter") or "?"
        chapter_title = attrs.get("title")   or ""

        for rel in data.get("relationships", []):
            if rel.get("type") == "manga":
                manga_id   = rel.get("id", "")
                rel_attrs  = rel.get("attributes") or {}
                titles     = rel_attrs.get("title", {})
                manga_title = (
                    titles.get("en")
                    or titles.get("ja-ro")
                    or next(iter(titles.values()), "")
                )
                break

    except Exception as exc:
        log.warning("[downloader] Could not fetch chapter metadata: %s", exc)

    # ── Cover art (best-effort, separate API call) ─────────────────────────────
    if manga_id:
        try:
            cresp = await client.get(
                f"{_MD_API}/cover",
                params={"manga[]": manga_id, "limit": 1, "order[volume]": "asc"},
            )
            cresp.raise_for_status()
            covers = cresp.json().get("data", [])
            if covers:
                fname     = covers[0]["attributes"]["fileName"]
                cover_url = f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
        except Exception as exc:
            log.debug("[downloader] Could not fetch cover for manga %s: %s", manga_id, exc)

    # ── Human-readable label ───────────────────────────────────────────────────
    parts = [manga_title] if manga_title else []
    parts.append(f"Ch. {chapter_num}")
    if chapter_title:
        parts.append(f"— {chapter_title}")
    label = " ".join(parts) or chapter_id

    return {
        "label":         label,
        "mangadex_id":   chapter_id,
        "manga_id":      manga_id,
        "manga_title":   manga_title,
        "chapter_num":   chapter_num,
        "chapter_title": chapter_title,
        "cover_url":     cover_url,
    }


async def _fetch_server_info(
    client:     httpx.AsyncClient,
    chapter_id: str,
    *,
    data_saver: bool,
) -> tuple[str, str, list[str]]:
    """
    Query the MangaDex@Home server endpoint.

    Returns (base_url, img_hash, filenames).
    """
    resp = await client.get(f"{_MD_API}/at-home/server/{chapter_id}")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"MangaDex API error for chapter {chapter_id}: "
            f"HTTP {exc.response.status_code}\n"
            f"  Response: {exc.response.text[:300]}"
        ) from exc

    body     = resp.json()
    base_url = body.get("baseUrl", "").rstrip("/")
    chapter  = body.get("chapter", {})
    img_hash = chapter.get("hash", "")

    filenames: list[str] = (
        chapter.get("dataSaver", []) if data_saver
        else chapter.get("data", [])
    )

    if not base_url or not img_hash or not filenames:
        raise RuntimeError(
            f"Unexpected MangaDex@Home response for chapter {chapter_id}:\n"
            f"  baseUrl={base_url!r}, hash={img_hash!r}, "
            f"pages={len(filenames)}"
        )

    return base_url, img_hash, filenames


# ---------------------------------------------------------------------------
# Image download with retry
# ---------------------------------------------------------------------------

async def _download_image(
    client:   httpx.AsyncClient,
    url:      str,
    out_path: Path,
    attempt:  int,
) -> None:
    """
    Download one image to out_path.  Retries up to _MAX_RETRIES times on
    transient errors.  Converts to PNG via Pillow so the pipeline always
    receives consistent format regardless of the source (JPEG/WebP/etc.).
    """
    try:
        resp = await client.get(url)
        resp.raise_for_status()

        # Convert to PNG so downstream pipeline always sees .png
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.save(str(out_path), format="PNG", optimize=False)

        log.debug("[downloader] Saved page → %s", out_path.name)

    except Exception as exc:
        if attempt < _MAX_RETRIES:
            wait = attempt * 2.0
            log.warning(
                "[downloader] Page %s failed (attempt %d/%d): %s — retrying in %.0f s",
                out_path.name, attempt, _MAX_RETRIES, exc, wait,
            )
            await asyncio.sleep(wait)
            await _download_image(client, url, out_path, attempt + 1)
        else:
            raise RuntimeError(
                f"Failed to download page after {_MAX_RETRIES} attempts: {url}\n"
                f"  Last error: {exc}"
            ) from exc
