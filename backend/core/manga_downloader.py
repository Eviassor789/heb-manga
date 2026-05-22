"""
Multi-source Chapter Downloader

Supports:
  • MangaDex  — https://mangadex.org/chapter/{uuid}  or bare UUID
  • WeebCentral — https://weebcentral.com/chapters/{ulid}

Both sources download all pages as numbered PNGs into <job_dir>/original/
and write a chapter_meta.json so the library can register the result.

MangaDex public API: https://api.mangadex.org/docs/
WeebCentral: uses a combination of API endpoints and HTML scraping (CORS-free
             server-side only).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from pathlib import Path

import httpx

from core.job_manager import EmitFn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MD_API     = "https://api.mangadex.org"
_WC_BASE    = "https://weebcentral.com"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
# WeebCentral series/chapter IDs are 26-char ULID-style strings
_WC_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}", re.IGNORECASE)
# URL matchers
_WC_CHAPTER_URL_RE = re.compile(r"weebcentral\.com/chapters/", re.IGNORECASE)

_INTER_PAGE_DELAY = 0.35   # seconds between CDN requests
_REQUEST_TIMEOUT  = 30.0
_MAX_RETRIES      = 3

_MD_HEADERS = {"User-Agent": "HebrewMangaTranslator/0.1"}
_WC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://weebcentral.com/",
}

# ---------------------------------------------------------------------------
# Public entrypoint — dispatches to MangaDex or WeebCentral
# ---------------------------------------------------------------------------

async def download_chapter(
    url_or_id: str,
    job_dir:   Path,
    emit:      EmitFn,
    *,
    data_saver: bool = False,
) -> list[Path]:
    """
    Download all pages of a chapter to <job_dir>/original/.

    Auto-detects the source from the URL:
      • weebcentral.com/chapters/...  → WeebCentral
      • everything else               → MangaDex (UUID required)

    Returns a sorted list of PNG paths for the next pipeline step.
    """
    if _WC_CHAPTER_URL_RE.search(url_or_id):
        return await _download_weebcentral(url_or_id, job_dir, emit)
    return await _download_mangadex(url_or_id, job_dir, emit, data_saver=data_saver)


def extract_chapter_id(url: str) -> str:
    """
    Extract a unique chapter identifier from a URL (used for cache lookup).

    MangaDex  → returns the bare UUID.
    WeebCentral → returns "wc:{ulid}" so it never collides with MangaDex IDs.

    Raises ValueError if the URL cannot be parsed.
    """
    if _WC_CHAPTER_URL_RE.search(url):
        m = _WC_ID_RE.search(url)
        if not m:
            raise ValueError(f"Cannot extract WeebCentral chapter ID from: {url!r}")
        return f"wc:{m.group(0).upper()}"
    return _extract_uuid(url)


# ---------------------------------------------------------------------------
# MangaDex downloader
# ---------------------------------------------------------------------------

async def _download_mangadex(
    url_or_id: str,
    job_dir:   Path,
    emit:      EmitFn,
    *,
    data_saver: bool,
) -> list[Path]:
    chapter_id = _extract_uuid(url_or_id)
    await emit({"stage": "download", "status": "starting", "chapter_id": chapter_id})

    async with httpx.AsyncClient(
        headers=_MD_HEADERS, timeout=_REQUEST_TIMEOUT, follow_redirects=True,
    ) as client:
        (chapter_meta, (base_url, img_hash, filenames)) = await asyncio.gather(
            _md_fetch_metadata(client, chapter_id),
            _md_fetch_server(client, chapter_id, data_saver=data_saver),
        )
        chapter_label = chapter_meta["label"]
        total = len(filenames)
        if total == 0:
            raise RuntimeError(f"Chapter {chapter_id} has no pages.")

        log.info("[downloader-md] '%s' — %d pages", chapter_label, total)

        quality_dir = "data-saver" if data_saver else "data"
        out_dir = job_dir / "original"
        out_dir.mkdir(parents=True, exist_ok=True)
        pages: list[Path] = []

        for i, filename in enumerate(filenames, 1):
            img_url  = f"{base_url}/{quality_dir}/{img_hash}/{filename}"
            out_path = out_dir / f"{i:03d}.png"
            await _download_image(client, img_url, out_path, attempt=1)
            pages.append(out_path)
            await emit({
                "stage": "download", "status": "running",
                "page": i, "total": total, "chapter_title": chapter_label,
            })
            if i < total:
                await asyncio.sleep(_INTER_PAGE_DELAY)

    _write_meta(job_dir, chapter_label, {
        "mangadex_id":   chapter_id,
        "manga_id":      chapter_meta.get("manga_id", ""),
        "manga_title":   chapter_meta.get("manga_title", ""),
        "chapter_num":   chapter_meta.get("chapter_num", ""),
        "chapter_title": chapter_meta.get("chapter_title", ""),
        "cover_url":     chapter_meta.get("cover_url", ""),
    })

    await emit({"stage": "download", "status": "done",
                "total_pages": total, "chapter_title": chapter_label})
    return sorted(pages)


async def _md_fetch_metadata(client: httpx.AsyncClient, chapter_id: str) -> dict:
    manga_id = manga_title = chapter_title = cover_url = ""
    chapter_num = "?"

    try:
        r = await client.get(f"{_MD_API}/chapter/{chapter_id}", params={"includes[]": ["manga"]})
        r.raise_for_status()
        data  = r.json().get("data", {})
        attrs = data.get("attributes", {})
        chapter_num   = attrs.get("chapter") or "?"
        chapter_title = attrs.get("title")   or ""
        for rel in data.get("relationships", []):
            if rel.get("type") == "manga":
                manga_id = rel.get("id", "")
                titles   = (rel.get("attributes") or {}).get("title", {})
                manga_title = (
                    titles.get("en") or titles.get("ja-ro")
                    or next(iter(titles.values()), "")
                )
                break
    except Exception as exc:
        log.warning("[downloader-md] metadata error: %s", exc)

    if manga_id:
        try:
            cr = await client.get(
                f"{_MD_API}/cover",
                params={"manga[]": manga_id, "limit": 1, "order[volume]": "asc"},
            )
            cr.raise_for_status()
            covers = cr.json().get("data", [])
            if covers:
                fname = covers[0]["attributes"]["fileName"]
                cover_url = f"https://uploads.mangadex.org/covers/{manga_id}/{fname}.512.jpg"
        except Exception as exc:
            log.debug("[downloader-md] cover error: %s", exc)

    parts = ([manga_title] if manga_title else []) + [f"Ch. {chapter_num}"]
    if chapter_title:
        parts.append(f"— {chapter_title}")
    label = " ".join(parts) or chapter_id

    return dict(
        label=label, mangadex_id=chapter_id, manga_id=manga_id,
        manga_title=manga_title, chapter_num=chapter_num,
        chapter_title=chapter_title, cover_url=cover_url,
    )


async def _md_fetch_server(
    client: httpx.AsyncClient, chapter_id: str, *, data_saver: bool,
) -> tuple[str, str, list[str]]:
    r = await client.get(f"{_MD_API}/at-home/server/{chapter_id}")
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"MangaDex API HTTP {exc.response.status_code} for chapter {chapter_id}"
        ) from exc

    body     = r.json()
    base_url = body.get("baseUrl", "").rstrip("/")
    chapter  = body.get("chapter", {})
    img_hash = chapter.get("hash", "")
    filenames: list[str] = (
        chapter.get("dataSaver", []) if data_saver else chapter.get("data", [])
    )
    if not base_url or not img_hash or not filenames:
        raise RuntimeError(f"Unexpected MangaDex@Home response for chapter {chapter_id}")
    return base_url, img_hash, filenames


# ---------------------------------------------------------------------------
# WeebCentral downloader
# ---------------------------------------------------------------------------

async def _download_weebcentral(
    url: str,
    job_dir: Path,
    emit: EmitFn,
) -> list[Path]:
    """
    Download a WeebCentral chapter.

    Image URLs are found via two strategies (tried in order):
      1. GET /api/chapters/{id}/images  → JSON array of URLs
      2. Scrape <img> tags from the chapter reader page

    Metadata is scraped from the chapter page (series title, chapter number).
    """
    m = _WC_ID_RE.search(url)
    if not m:
        raise ValueError(f"Cannot extract WeebCentral chapter ID from: {url!r}")
    chapter_id = m.group(0).upper()

    await emit({"stage": "download", "status": "starting"})

    async with httpx.AsyncClient(
        headers=_WC_HEADERS, timeout=_REQUEST_TIMEOUT, follow_redirects=True,
    ) as client:
        # ── Fetch metadata and image list in parallel ──────────────────────
        meta_task  = asyncio.create_task(_wc_fetch_meta(client, chapter_id))
        images_task = asyncio.create_task(_wc_fetch_images(client, chapter_id))

        meta   = await meta_task
        images = await images_task

    if not images:
        raise RuntimeError(
            f"No page images found for WeebCentral chapter {chapter_id}.\n"
            "The site may have changed its image delivery format."
        )

    chapter_label = meta.get("label", f"WeebCentral Ch. {chapter_id}")
    total = len(images)
    log.info("[downloader-wc] '%s' — %d pages", chapter_label, total)

    out_dir = job_dir / "original"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []

    async with httpx.AsyncClient(
        headers=_WC_HEADERS, timeout=_REQUEST_TIMEOUT, follow_redirects=True,
    ) as client:
        for i, img_url in enumerate(images, 1):
            out_path = out_dir / f"{i:03d}.png"
            await _download_image(client, img_url, out_path, attempt=1)
            pages.append(out_path)
            await emit({
                "stage": "download", "status": "running",
                "page": i, "total": total, "chapter_title": chapter_label,
            })
            if i < total:
                await asyncio.sleep(_INTER_PAGE_DELAY)

    # Use "wc:" prefix so WeebCentral IDs never collide with MangaDex UUIDs
    _write_meta(job_dir, chapter_label, {
        "mangadex_id":   f"wc:{chapter_id}",   # re-using the field as a generic source_id
        "manga_id":      meta.get("series_id", ""),
        "manga_title":   meta.get("series_title", ""),
        "chapter_num":   meta.get("chapter_num", ""),
        "chapter_title": meta.get("chapter_title", ""),
        "cover_url":     meta.get("cover_url", ""),
    })

    await emit({"stage": "download", "status": "done",
                "total_pages": total, "chapter_title": chapter_label})
    return sorted(pages)


async def _wc_fetch_images(client: httpx.AsyncClient, chapter_id: str) -> list[str]:
    """
    Return the list of page image URLs for a WeebCentral chapter.

    WeebCentral uses HTMX: the main reader page has NO <img> tags.  On load,
    the browser fires a GET to /chapters/{id}/images?… which returns a plain
    HTML fragment containing all <img src="https://cdn…"> tags.  We replicate
    that HTMX request here.

    URL:     GET /chapters/{id}/images?is_prev=False&current_page=1&reading_style=long_strip
    Headers: HX-Request: true  (required — without it the server returns the full page)
             Referer: https://weebcentral.com/chapters/{id}
    """
    from bs4 import BeautifulSoup  # noqa: PLC0415

    htmx_headers = {
        **_WC_HEADERS,
        "Accept":      "text/html, */*",
        "HX-Request":  "true",
        "Referer":     f"{_WC_BASE}/chapters/{chapter_id}",
    }
    images_url = (
        f"{_WC_BASE}/chapters/{chapter_id}/images"
        f"?is_prev=False&current_page=1&reading_style=long_strip"
    )

    try:
        r = await client.get(images_url, headers=htmx_headers)
        if r.is_success:
            soup = BeautifulSoup(r.text, "html.parser")
            urls: list[str] = []
            for img in soup.find_all("img"):
                src = (img.get("src") or img.get("data-src") or "").strip()
                if src and _is_manga_page_url(src):
                    urls.append(src)
            if urls:
                log.info(
                    "[downloader-wc] HTMX images endpoint returned %d pages for %s",
                    len(urls), chapter_id,
                )
                return urls
            log.warning(
                "[downloader-wc] HTMX endpoint returned success but no valid img tags "
                "(chapter %s).  Response preview: %s", chapter_id, r.text[:300],
            )
        else:
            log.warning(
                "[downloader-wc] HTMX images endpoint returned HTTP %s for chapter %s",
                r.status_code, chapter_id,
            )
    except Exception as exc:
        log.warning("[downloader-wc] HTMX images request failed for %s: %s", chapter_id, exc)

    return []


def _is_manga_page_url(url: str) -> bool:
    """Heuristic: does this URL look like a manga page (not a nav icon/logo)?"""
    # Relative URLs (e.g. /static/images/brand.png) are always site assets, never CDN pages
    if not url.startswith(("http://", "https://")):
        return False
    url_lower = url.lower()
    for skip in ("logo", "icon", "avatar", "banner", "thumb-sm", "favicon", "sprite", "brand"):
        if skip in url_lower:
            return False
    for good in ("compsci88", "/chapters/", "/manga/", "/page/", "/pages/"):
        if good in url_lower:
            return True
    # Accept absolute URL with a common image extension
    return bool(re.search(r'\.(jpe?g|png|webp)(\?|$)', url_lower))


async def _wc_fetch_meta(client: httpx.AsyncClient, chapter_id: str) -> dict:
    """
    Scrape the WeebCentral chapter page to extract series title, series ID,
    and chapter number for library registration.
    """
    series_id = series_title = chapter_num = chapter_title = cover_url = ""

    try:
        r = await client.get(f"{_WC_BASE}/chapters/{chapter_id}")
        if not r.is_success:
            return {}

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        # Series link — e.g. <a href="/series/01J76...">Hunter x Hunter</a>
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "/series/" in href:
                sid_m = _WC_ID_RE.search(href)
                if sid_m:
                    series_id    = sid_m.group(0).upper()
                    series_title = link.get_text(strip=True)
                    break

        # Chapter number from <title> or breadcrumb
        page_title = soup.find("title")
        if page_title:
            t = page_title.get_text(strip=True)
            num_m = re.search(r"chapter\s*([\d.]+)", t, re.IGNORECASE)
            if num_m:
                chapter_num = num_m.group(1)

        # og:image for cover
        og = soup.find("meta", property="og:image")
        if og:
            cover_url = og.get("content", "").strip()

    except Exception as exc:
        log.debug("[downloader-wc] meta fetch error: %s", exc)

    parts = ([series_title] if series_title else [])
    if chapter_num:
        parts.append(f"Ch. {chapter_num}")
    label = " ".join(parts) or f"WeebCentral {chapter_id[:8]}"

    return dict(
        label=label, series_id=series_id, series_title=series_title,
        chapter_num=chapter_num, chapter_title=chapter_title, cover_url=cover_url,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_uuid(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    match = _UUID_RE.search(url_or_id)
    if not match:
        raise ValueError(
            f"Cannot extract a MangaDex UUID from: {url_or_id!r}\n"
            "  Expected: https://mangadex.org/chapter/<uuid>  or  <bare-uuid>"
        )
    return match.group(0).lower()


def _write_meta(job_dir: Path, chapter_label: str, meta: dict) -> None:
    """Persist chapter metadata for library registration after pipeline completes."""
    (job_dir / "chapter_title.txt").write_text(chapter_label, encoding="utf-8")
    (job_dir / "chapter_meta.json").write_text(
        _json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _download_image(
    client:   httpx.AsyncClient,
    url:      str,
    out_path: Path,
    attempt:  int,
) -> None:
    """
    Download one image to out_path as PNG.
    Converts JPEG/WebP/etc. via Pillow so the pipeline always gets .png.
    Retries up to _MAX_RETRIES times on transient errors.
    """
    try:
        resp = await client.get(url)
        resp.raise_for_status()

        import io
        from PIL import Image
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.save(str(out_path), format="PNG", optimize=False)
        log.debug("[downloader] Saved %s", out_path.name)

    except Exception as exc:
        if attempt < _MAX_RETRIES:
            wait = attempt * 2.0
            log.warning(
                "[downloader] %s failed (attempt %d/%d): %s — retry in %.0f s",
                out_path.name, attempt, _MAX_RETRIES, exc, wait,
            )
            await asyncio.sleep(wait)
            await _download_image(client, url, out_path, attempt + 1)
        else:
            raise RuntimeError(
                f"Failed after {_MAX_RETRIES} attempts: {url}\n  Last error: {exc}"
            ) from exc
