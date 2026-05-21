"""
Library — shared chapter cache backed by Supabase (metadata) + Cloudflare R2 (files).

Every MangaDex chapter that completes translation is:
  1. Compressed PDF + individual JPEG pages uploaded to Cloudflare R2
  2. Metadata row upserted in Supabase so future requests return cached result instantly

The library is silently disabled if any required env var is missing — the pipeline
works exactly as before.  Set all vars to enable:

  SUPABASE_URL        https://xxxx.supabase.co
  SUPABASE_KEY        service_role key  (secret — backend only, never expose to clients)
  R2_ACCOUNT_ID       Cloudflare account ID (32-char hex, visible in dashboard)
  R2_ACCESS_KEY_ID    R2 API token Access Key ID
  R2_SECRET_KEY       R2 API token Secret Access Key
  R2_BUCKET           Bucket name, e.g. manga-chapters
  R2_PUBLIC_URL       Public bucket URL, e.g. https://pub-xxxx.r2.dev
                      Enable "Public access" on the bucket in Cloudflare R2 dashboard.

Supabase table (run once in SQL Editor > New Query):
──────────────────────────────────────────────────────────────────────────────
  CREATE TABLE IF NOT EXISTS chapters (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    mangadex_id    TEXT         UNIQUE NOT NULL,
    manga_id       TEXT,
    manga_title    TEXT         NOT NULL,
    chapter_num    TEXT,
    chapter_title  TEXT,
    cover_url      TEXT,
    page_count     INT,
    pdf_url        TEXT,
    pages_prefix   TEXT,
    pdf_size_kb    INT,
    status         TEXT         NOT NULL DEFAULT 'done',
    translated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
  );

  ALTER TABLE chapters ENABLE ROW LEVEL SECURITY;
  CREATE POLICY "public_read" ON chapters FOR SELECT TO anon USING (true);
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config + feature flag
# ---------------------------------------------------------------------------

_REQUIRED_VARS = (
    "SUPABASE_URL", "SUPABASE_KEY",
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_KEY",
    "R2_BUCKET", "R2_PUBLIC_URL",
)

_UPLOAD_CONCURRENCY = 5   # max parallel R2 uploads (avoid rate limits)


def library_enabled() -> bool:
    """Return True only when every required env var is non-empty."""
    return all(os.getenv(k, "").strip() for k in _REQUIRED_VARS)


def _env(key: str) -> str:
    return os.getenv(key, "").strip()


# ---------------------------------------------------------------------------
# Supabase client singleton (thread-safe, lazy)
# ---------------------------------------------------------------------------

_sb_client = None
_sb_lock   = threading.Lock()


def _get_sb():
    global _sb_client
    if _sb_client is None:
        with _sb_lock:
            if _sb_client is None:
                from supabase import create_client  # noqa: PLC0415
                _sb_client = create_client(_env("SUPABASE_URL"), _env("SUPABASE_KEY"))
    return _sb_client


# ---------------------------------------------------------------------------
# R2 upload (S3-compatible API)
# ---------------------------------------------------------------------------

def _r2_endpoint() -> str:
    return f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"


async def _upload_bytes(key: str, data: bytes, content_type: str) -> str:
    """Upload bytes to R2 and return the public URL."""
    import aioboto3  # noqa: PLC0415

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=_r2_endpoint(),
        aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("R2_SECRET_KEY"),
        region_name="auto",
    ) as s3:
        await s3.put_object(
            Bucket=_env("R2_BUCKET"),
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    return f"{_env('R2_PUBLIC_URL').rstrip('/')}/{key}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_cache(mangadex_id: str) -> Optional[dict]:
    """
    Query Supabase for a completed translation of this MangaDex chapter.

    Returns the full chapter row dict on a cache hit, or None when the chapter
    hasn't been translated yet / library is disabled / Supabase unreachable.
    """
    if not library_enabled():
        return None
    try:
        sb = _get_sb()
        result = await asyncio.to_thread(
            lambda: (
                sb.table("chapters")
                .select("*")
                .eq("mangadex_id", mangadex_id)
                .eq("status", "done")
                .limit(1)
                .execute()
            )
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as exc:
        log.warning("[library] check_cache failed: %s", exc)
        return None


async def register_chapter(
    *,
    mangadex_id:   str,
    manga_title:   str,
    manga_id:      str,
    chapter_num:   str,
    chapter_title: str,
    cover_url:     str,
    page_count:    int,
    pdf_url:       str,
    pages_prefix:  str,
    pdf_size_kb:   int,
) -> Optional[str]:
    """
    Upsert a completed chapter into Supabase.
    Returns the row's UUID (string) or None on failure.
    """
    if not library_enabled():
        return None
    try:
        sb = _get_sb()
        result = await asyncio.to_thread(
            lambda: (
                sb.table("chapters")
                .upsert(
                    {
                        "mangadex_id":   mangadex_id,
                        "manga_title":   manga_title,
                        "manga_id":      manga_id,
                        "chapter_num":   chapter_num,
                        "chapter_title": chapter_title,
                        "cover_url":     cover_url,
                        "page_count":    page_count,
                        "pdf_url":       pdf_url,
                        "pages_prefix":  pages_prefix,
                        "pdf_size_kb":   pdf_size_kb,
                        "status":        "done",
                    },
                    on_conflict="mangadex_id",
                )
                .execute()
            )
        )
        rows = result.data or []
        if rows:
            lid = rows[0]["id"]
            log.info("[library] Registered chapter %s → library id=%s", mangadex_id, lid)
            return lid
        return None
    except Exception as exc:
        log.error("[library] register_chapter failed: %s", exc)
        return None


async def list_chapters(limit: int = 200) -> list[dict]:
    """
    Return all completed chapters from Supabase, newest first.
    Returns an empty list when library is disabled or on error.
    """
    if not library_enabled():
        return []
    try:
        sb = _get_sb()
        result = await asyncio.to_thread(
            lambda: (
                sb.table("chapters")
                .select("*")
                .eq("status", "done")
                .order("translated_at", desc=True)
                .limit(limit)
                .execute()
            )
        )
        return result.data or []
    except Exception as exc:
        log.error("[library] list_chapters failed: %s", exc)
        return []


async def get_chapter(chapter_id: str) -> Optional[dict]:
    """Fetch a single chapter by its Supabase UUID. Returns None on miss/error."""
    if not library_enabled():
        return None
    try:
        sb = _get_sb()
        result = await asyncio.to_thread(
            lambda: (
                sb.table("chapters")
                .select("*")
                .eq("id", chapter_id)
                .single()
                .execute()
            )
        )
        return result.data
    except Exception as exc:
        log.warning("[library] get_chapter %s failed: %s", chapter_id, exc)
        return None


async def upload_chapter_files(
    job_dir: Path,
    mangadex_id: str,
) -> tuple[str, str, int, int]:
    """
    Build compressed PDF + JPEG page images (if not already on disk),
    then upload both to Cloudflare R2.

    Returns (pdf_url, pages_prefix, page_count, pdf_size_kb).

    pages_prefix is the R2 directory URL; clients append /001.jpg, /002.jpg, …
    to get individual page images for the web reader.
    """
    from core.pdf_utils import build_compressed_pdf  # noqa: PLC0415

    output_dir = job_dir / "output"
    pages_dir  = output_dir / "pages"
    pdf_path   = output_dir / "result_compressed.pdf"

    # ── Build PDF + extract JPEG pages (single pass) ───────────────────────────
    pages_exist = pages_dir.exists() and bool(list(pages_dir.glob("*.jpg")))
    pdf_exists  = pdf_path.exists()

    if not pdf_exists or not pages_exist:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: build_compressed_pdf(output_dir, pdf_path, quality=85, save_pages=True),
        )
    elif pdf_exists and not pages_exist:
        # PDF already built on a prior download — extract JPEG pages separately
        import io as _io
        from PIL import Image as _Img  # noqa: PLC0415

        page_pngs = sorted(p for p in output_dir.glob("*.png") if p.stem.isdigit())
        pages_dir.mkdir(exist_ok=True)
        for p in page_pngs:
            img = _Img.open(p).convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True, subsampling=2)
            (pages_dir / f"{p.stem}.jpg").write_bytes(buf.getvalue())

    page_paths = sorted(pages_dir.glob("*.jpg"))
    page_count = len(page_paths)

    # ── Upload PDF ─────────────────────────────────────────────────────────────
    pdf_bytes   = pdf_path.read_bytes()
    pdf_key     = f"chapters/{mangadex_id}/compressed.pdf"
    pdf_url     = await _upload_bytes(pdf_key, pdf_bytes, "application/pdf")
    pdf_size_kb = len(pdf_bytes) // 1024
    log.info("[library] Uploaded PDF (%d KB) → %s", pdf_size_kb, pdf_url)

    # ── Upload page images concurrently ───────────────────────────────────────
    pages_key_prefix = f"chapters/{mangadex_id}/pages"
    sem = asyncio.Semaphore(_UPLOAD_CONCURRENCY)

    async def _upload_page(path: Path) -> None:
        async with sem:
            await _upload_bytes(
                f"{pages_key_prefix}/{path.name}",
                path.read_bytes(),
                "image/jpeg",
            )

    await asyncio.gather(*(_upload_page(p) for p in page_paths))
    log.info("[library] Uploaded %d page images for %s", page_count, mangadex_id)

    pages_prefix = f"{_env('R2_PUBLIC_URL').rstrip('/')}/{pages_key_prefix}"
    return pdf_url, pages_prefix, page_count, pdf_size_kb
