"""
Library — three operating modes depending on which env vars are set:

──────────────────────────────────────────────────────────────────────────────
LOCAL (default — zero config)
  Metadata : SQLite at data/library.db
  Files    : disk, served by FastAPI at /api/library/local-pages/{job_id}/…
──────────────────────────────────────────────────────────────────────────────
HYBRID (R2 vars only)                    ← recommended for production
  Metadata : SQLite at data/library.db
  Files    : Cloudflare R2 CDN (permanent public URLs, 10 GB free / 0 egress)
  Required env vars (backend/.env):
      R2_ACCOUNT_ID       Cloudflare account ID
      R2_ACCESS_KEY_ID    R2 API token — Access Key ID
      R2_SECRET_KEY       R2 API token — Secret Access Key
      R2_BUCKET           bucket name, e.g. manga-chapters
      R2_PUBLIC_URL       public URL of the bucket, e.g. https://pub-xxxx.r2.dev
──────────────────────────────────────────────────────────────────────────────
FULL CLOUD (R2 + Supabase)
  Metadata : Supabase (PostgreSQL)
  Files    : Cloudflare R2
  Required env vars: all R2 vars above PLUS
      SUPABASE_URL        https://xxxx.supabase.co
      SUPABASE_KEY        service_role key

  Supabase table (run once in the SQL editor):
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
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

_R2_VARS = (
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_KEY",
    "R2_BUCKET", "R2_PUBLIC_URL",
)
_SUPABASE_VARS = ("SUPABASE_URL", "SUPABASE_KEY")


def _r2_mode() -> bool:
    """True when all R2 credentials are present → files go to Cloudflare R2 CDN."""
    return all(os.getenv(k, "").strip() for k in _R2_VARS)


def _supabase_mode() -> bool:
    """True when Supabase credentials are present → metadata goes to PostgreSQL."""
    return all(os.getenv(k, "").strip() for k in _SUPABASE_VARS)


def _cloud_mode() -> bool:
    """True when BOTH R2 AND Supabase are configured (full cloud)."""
    return _r2_mode() and _supabase_mode()


def library_enabled() -> bool:
    """Always True — the library works in all three modes."""
    return True


def _env(key: str) -> str:
    return os.getenv(key, "").strip()


# ---------------------------------------------------------------------------
# LOCAL mode — SQLite backend
# ---------------------------------------------------------------------------

_LOCAL_DB_PATH = Path("data/library.db")
_local_db_conn: Optional[sqlite3.Connection] = None
_local_db_lock = threading.Lock()


def _get_local_db() -> sqlite3.Connection:
    """Return a shared SQLite connection, creating the schema on first call."""
    global _local_db_conn
    if _local_db_conn is None:
        with _local_db_lock:
            if _local_db_conn is None:
                _LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(_LOCAL_DB_PATH),
                    check_same_thread=False,  # protected by _local_db_lock on writes
                )
                conn.row_factory = sqlite3.Row
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chapters (
                        id            TEXT PRIMARY KEY,
                        mangadex_id   TEXT UNIQUE NOT NULL,
                        manga_id      TEXT,
                        manga_title   TEXT NOT NULL,
                        chapter_num   TEXT,
                        chapter_title TEXT,
                        cover_url     TEXT,
                        page_count    INT,
                        pdf_url       TEXT,
                        pages_prefix  TEXT,
                        pdf_size_kb   INT,
                        status        TEXT NOT NULL DEFAULT 'done',
                        translated_at TEXT NOT NULL
                    )
                """)
                conn.commit()
                _local_db_conn = conn
    return _local_db_conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _local_check_cache(mangadex_id: str) -> Optional[dict]:
    db = _get_local_db()
    with _local_db_lock:
        cur = db.execute(
            "SELECT * FROM chapters WHERE mangadex_id = ? AND status = 'done' LIMIT 1",
            (mangadex_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def _local_register_chapter(data: dict) -> str:
    """Upsert a chapter row in SQLite. Returns the row id."""
    db = _get_local_db()
    with _local_db_lock:
        cur = db.execute(
            "SELECT id FROM chapters WHERE mangadex_id = ?", (data["mangadex_id"],)
        )
        existing = cur.fetchone()
        row_id = existing["id"] if existing else str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO chapters
              (id, mangadex_id, manga_id, manga_title, chapter_num, chapter_title,
               cover_url, page_count, pdf_url, pages_prefix, pdf_size_kb, status, translated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mangadex_id) DO UPDATE SET
              manga_title   = excluded.manga_title,
              manga_id      = excluded.manga_id,
              chapter_num   = excluded.chapter_num,
              chapter_title = excluded.chapter_title,
              cover_url     = excluded.cover_url,
              page_count    = excluded.page_count,
              pdf_url       = excluded.pdf_url,
              pages_prefix  = excluded.pages_prefix,
              pdf_size_kb   = excluded.pdf_size_kb,
              status        = excluded.status,
              translated_at = excluded.translated_at
            """,
            (
                row_id,
                data["mangadex_id"],
                data.get("manga_id", ""),
                data["manga_title"],
                data.get("chapter_num", ""),
                data.get("chapter_title", ""),
                data.get("cover_url", ""),
                data.get("page_count", 0),
                data.get("pdf_url", ""),
                data.get("pages_prefix", ""),
                data.get("pdf_size_kb", 0),
                "done",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    return row_id


def _local_list_chapters(limit: int = 200) -> list[dict]:
    db = _get_local_db()
    with _local_db_lock:
        cur = db.execute(
            "SELECT * FROM chapters WHERE status = 'done' ORDER BY translated_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def _local_list_by_manga(manga_id: str) -> list[dict]:
    db = _get_local_db()
    with _local_db_lock:
        cur = db.execute(
            """
            SELECT id, mangadex_id, chapter_num, chapter_title
            FROM chapters
            WHERE manga_id = ? AND status = 'done'
            ORDER BY translated_at DESC
            """,
            (manga_id,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def _local_get_chapter(chapter_id: str) -> Optional[dict]:
    db = _get_local_db()
    with _local_db_lock:
        cur = db.execute("SELECT * FROM chapters WHERE id = ? LIMIT 1", (chapter_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


async def _local_upload_chapter_files(
    job_dir: Path,
    mangadex_id: str,
) -> tuple[str, str, int, int]:
    """
    In local mode: build compressed PDF + JPEG pages on disk (no cloud upload).
    Returns local FastAPI URLs so the reader can load pages without any external service.
    """
    from core.pdf_utils import build_compressed_pdf  # noqa: PLC0415

    output_dir = job_dir / "output"
    pages_dir  = output_dir / "pages"
    pdf_path   = output_dir / "result_compressed.pdf"

    pages_exist = pages_dir.exists() and bool(list(pages_dir.glob("*.jpg")))
    pdf_exists  = pdf_path.exists()

    if not pdf_exists or not pages_exist:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: build_compressed_pdf(output_dir, pdf_path, save_pages=True),
        )

    page_paths  = sorted(pages_dir.glob("*.jpg")) if pages_dir.exists() else []
    page_count  = len(page_paths)
    pdf_size_kb = int(pdf_path.stat().st_size / 1024) if pdf_path.exists() else 0

    job_id       = job_dir.name
    pdf_url      = f"/api/jobs/{job_id}/download?compressed=true"
    pages_prefix = f"/api/library/local-pages/{job_id}"

    return pdf_url, pages_prefix, page_count, pdf_size_kb


# ---------------------------------------------------------------------------
# CLOUD mode — Supabase + R2 backend (unchanged from original)
# ---------------------------------------------------------------------------

_UPLOAD_CONCURRENCY = 5

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


def _r2_endpoint() -> str:
    return f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"


async def _upload_bytes(key: str, data: bytes, content_type: str) -> str:
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
# Public API — dispatches to local or cloud backend
# ---------------------------------------------------------------------------

async def check_cache(mangadex_id: str) -> Optional[dict]:
    """
    Return the completed library entry for this chapter ID, or None.
    Works in all three modes (local / hybrid / full-cloud).
    """
    try:
        if _supabase_mode():
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
        else:
            return await asyncio.to_thread(_local_check_cache, mangadex_id)
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
    Upsert a completed chapter into the library.
    Returns the row UUID string, or None on failure.
    """
    payload = dict(
        mangadex_id=mangadex_id, manga_title=manga_title, manga_id=manga_id,
        chapter_num=chapter_num, chapter_title=chapter_title, cover_url=cover_url,
        page_count=page_count, pdf_url=pdf_url, pages_prefix=pages_prefix,
        pdf_size_kb=pdf_size_kb,
    )
    try:
        if _supabase_mode():
            sb = _get_sb()
            result = await asyncio.to_thread(
                lambda: (
                    sb.table("chapters")
                    .upsert({**payload, "status": "done"}, on_conflict="mangadex_id")
                    .execute()
                )
            )
            rows = result.data or []
            if rows:
                lid = rows[0]["id"]
                log.info("[library] Supabase: registered %s → %s", mangadex_id, lid)
                return lid
            return None
        else:
            lid = await asyncio.to_thread(_local_register_chapter, payload)
            mode = "R2+SQLite" if _r2_mode() else "Local"
            log.info("[library] %s: registered %s → %s", mode, mangadex_id, lid)
            return lid
    except Exception as exc:
        log.error("[library] register_chapter failed: %s", exc)
        return None


async def list_chapters(limit: int = 200) -> list[dict]:
    """Return all completed chapters, newest first."""
    try:
        if _supabase_mode():
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
        else:
            return await asyncio.to_thread(_local_list_chapters, limit)
    except Exception as exc:
        log.error("[library] list_chapters failed: %s", exc)
        return []


async def list_chapters_by_manga(manga_id: str) -> list[dict]:
    """
    Return translated chapters for a specific manga_id (MangaDex manga UUID or WC series id),
    selecting only the columns needed by the manga detail page.  Newest-translated first.
    Works in both local (SQLite) and cloud (Supabase) modes.
    """
    try:
        if _supabase_mode():
            sb = _get_sb()
            result = await asyncio.to_thread(
                lambda: (
                    sb.table("chapters")
                    .select("id, mangadex_id, chapter_num, chapter_title")
                    .eq("manga_id", manga_id)
                    .eq("status", "done")
                    .execute()
                )
            )
            return result.data or []
        else:
            return await asyncio.to_thread(_local_list_by_manga, manga_id)
    except Exception as exc:
        log.warning("[library] list_chapters_by_manga(%s) failed: %s", manga_id, exc)
        return []


async def get_chapter(chapter_id: str) -> Optional[dict]:
    """Return a single chapter by its UUID, or None."""
    try:
        if _supabase_mode():
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
        else:
            return await asyncio.to_thread(_local_get_chapter, chapter_id)
    except Exception as exc:
        log.warning("[library] get_chapter %s failed: %s", chapter_id, exc)
        return None


async def upload_chapter_files(
    job_dir: Path,
    mangadex_id: str,
) -> tuple[str, str, int, int]:
    """
    Build compressed PDF + JPEG pages, upload to R2 (hybrid/cloud) or keep local.
    Returns (pdf_url, pages_prefix, page_count, pdf_size_kb).

    R2 upload happens whenever R2 credentials are configured — Supabase is NOT
    required.  This means the HYBRID mode (SQLite + R2) works out of the box.
    """
    if not _r2_mode():
        return await _local_upload_chapter_files(job_dir, mangadex_id)

    # ── Cloud mode: upload to R2 ───────────────────────────────────────────────
    from core.pdf_utils import build_compressed_pdf  # noqa: PLC0415

    output_dir = job_dir / "output"
    pages_dir  = output_dir / "pages"
    pdf_path   = output_dir / "result_compressed.pdf"

    pages_exist = pages_dir.exists() and bool(list(pages_dir.glob("*.jpg")))
    pdf_exists  = pdf_path.exists()

    if not pdf_exists or not pages_exist:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: build_compressed_pdf(output_dir, pdf_path, save_pages=True),
        )
    elif pdf_exists and not pages_exist:
        import io as _io
        from PIL import Image as _Img  # noqa: PLC0415
        page_pngs = sorted(p for p in output_dir.glob("*.png") if p.stem.isdigit())
        pages_dir.mkdir(exist_ok=True)
        for p in page_pngs:
            img = _Img.open(p).convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True, subsampling=2)
            (pages_dir / f"{p.stem}.jpg").write_bytes(buf.getvalue())

    page_paths = sorted(pages_dir.glob("*.jpg"))
    page_count = len(page_paths)

    pdf_bytes   = pdf_path.read_bytes()
    pdf_key     = f"chapters/{mangadex_id}/compressed.pdf"
    pdf_url     = await _upload_bytes(pdf_key, pdf_bytes, "application/pdf")
    pdf_size_kb = len(pdf_bytes) // 1024
    log.info("[library] Uploaded PDF (%d KB) → %s", pdf_size_kb, pdf_url)

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
