"""
Step 3 — Inpainting (Text Erasure)

Uses the binary masks produced by Step 1 (detector) to erase all text
regions from the original page images, producing clean artwork ready for
Hebrew typesetting.

Primary  : LaMa neural inpainting via simple-lama-inpainting
           — seamless fills on complex backgrounds (screen tones, gradients,
             cross-hatching). Model auto-downloads on first use (~200 MB).

Fallback : OpenCV TELEA algorithm
           — fast, dependency-free (already installed via EasyOCR),
             excellent on the white/light backgrounds of speech bubbles.
           — activated automatically if LaMa fails to load or crashes.

Mask convention (both backends):
    255  →  inpaint this pixel  (text region)
    0    →  keep this pixel     (artwork)

This matches the mask format written by detector.py.

Output: <job_dir>/cleaned/NNN.png  — page with text erased
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import json as _json

import cv2
import numpy as np
from PIL import Image

import os as _os

from core.job_manager import EmitFn

# Set USE_MODAL=true in backend/.env to offload inpainting to Modal GPU.
_USE_MODAL = _os.getenv("USE_MODAL", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Neighbourhood radius for OpenCV TELEA inpainting (pixels).
# 3-5 is the sweet spot for manga speech bubbles.
_CV2_RADIUS = 4

# ---------------------------------------------------------------------------
# LaMa singleton — lazy, thread-safe, fault-tolerant
# ---------------------------------------------------------------------------

_lama = None
_lama_ok: bool | None = None   # None = untested, True = works, False = broken
_lama_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpainter")


def _get_lama():
    """
    Return a cached SimpleLama instance, or None if LaMa is unavailable.

    First call triggers model download (~200 MB to HuggingFace cache).
    Any exception during load permanently sets _lama_ok=False so subsequent
    pages fall back to OpenCV without retrying the broken load.
    """
    global _lama, _lama_ok

    if _lama_ok is False:
        return None

    if _lama is None:
        with _lama_lock:
            if _lama is None:
                try:
                    from simple_lama_inpainting import SimpleLama  # noqa: PLC0415
                    _lama = SimpleLama()
                    _lama_ok = True
                    print("[inpainter] LaMa model loaded successfully.")
                except Exception as exc:
                    _lama_ok = False
                    print(f"[inpainter] LaMa unavailable ({exc}). Using OpenCV fallback.")

    return _lama


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def inpaint(
    job_dir: Path,
    pages: list[Path],
    emit: EmitFn,
    modal_token_id:     str | None = None,
    modal_token_secret: str | None = None,
) -> list[Path]:
    """
    Erase text regions from every page using the masks from Step 1.

    Writes cleaned images to <job_dir>/cleaned/.
    Uses Modal GPU when USE_MODAL=true (server-level) or when per-job BYOK
    credentials are supplied.  Falls back to local CPU/LaMa otherwise.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "inpaint", "status": "running"})

    detection_dir = job_dir / "detection"
    cleaned_dir   = job_dir / "cleaned"
    loop      = asyncio.get_running_loop()
    total     = len(pages)
    modal_seconds = 0.0
    use_modal = _USE_MODAL or bool(modal_token_id and modal_token_secret)

    # Pre-warm LaMa in the executor so any load errors surface once, cleanly,
    # before we start processing pages. If LaMa fails, _lama_ok=False and all
    # pages will use the OpenCV fallback without re-attempting the download.
    if not use_modal:
        await loop.run_in_executor(_executor, _get_lama)

    for i, page_path in enumerate(pages, start=1):
        mask_path = detection_dir / f"{page_path.stem}_mask.png"
        out_path  = cleaned_dir   / page_path.name

        try:
            if use_modal:
                t0 = time.perf_counter()
                await _inpaint_page_modal(page_path, mask_path, out_path, modal_token_id, modal_token_secret)
                modal_seconds += time.perf_counter() - t0
            else:
                await loop.run_in_executor(_executor, _inpaint_page, page_path, mask_path, out_path)
        except Exception as exc:
            # If inpainting fails for any reason, fall back to copying the original.
            # The typesetter will still add Hebrew text on top; the page stays readable.
            import logging as _log
            _log.getLogger(__name__).error(
                "[inpainter] Page %s failed (%s) — copying original as fallback", page_path.name, exc
            )
            await loop.run_in_executor(None, shutil.copy2, page_path, out_path)

        await emit({"stage": "inpaint", "status": "running", "page": i, "total": total})

    await emit({
        "stage": "inpaint", "status": "done", "total_pages": total,
        "modal_gpu_seconds": round(modal_seconds, 2),
    })
    return pages


# ---------------------------------------------------------------------------
# Public per-page entrypoint (used by the parallel pipeline in main.py)
# ---------------------------------------------------------------------------

async def inpaint_one_page(
    page_path: Path,
    job_dir: Path,
    modal_token_id:     str | None = None,
    modal_token_secret: str | None = None,
) -> float:
    """
    Inpaint a single page.  Writes cleaned/<page>.png.
    Returns Modal GPU seconds consumed (0.0 for local processing).
    """
    detection_dir = job_dir / "detection"
    cleaned_dir   = job_dir / "cleaned"
    mask_path     = detection_dir / f"{page_path.stem}_mask.png"
    out_path      = cleaned_dir   / page_path.name
    loop          = asyncio.get_running_loop()
    modal_seconds = 0.0
    use_modal     = _USE_MODAL or bool(modal_token_id and modal_token_secret)

    try:
        if use_modal:
            t0 = time.perf_counter()
            await _inpaint_page_modal(page_path, mask_path, out_path, modal_token_id, modal_token_secret)
            modal_seconds = time.perf_counter() - t0
        else:
            await loop.run_in_executor(_executor, _inpaint_page, page_path, mask_path, out_path)
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).error(
            "[inpainter] Page %s failed (%s) — copying original as fallback",
            page_path.name, exc,
        )
        await loop.run_in_executor(None, shutil.copy2, page_path, out_path)

    return modal_seconds


_modal_inpaint_fn = None   # cached for server-level Modal (USE_MODAL=true)
# Lazily created so it binds to the correct running event loop (not module-import time).
_modal_env_lock: asyncio.Lock | None = None


def _get_modal_env_lock() -> asyncio.Lock:
    """Return the per-module asyncio.Lock, creating it on first use."""
    global _modal_env_lock
    if _modal_env_lock is None:
        _modal_env_lock = asyncio.Lock()
    return _modal_env_lock


# Phrases the Modal SDK uses when token credentials are rejected.
_MODAL_AUTH_PHRASES = (
    "token id is malformed",
    "token secret is malformed",
    "invalid token",
    "authentication failed",
    "unauthorized",
    "credentials",
)


def _is_modal_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _MODAL_AUTH_PHRASES)


async def _inpaint_page_modal(
    page_path: Path,
    mask_path: Path,
    out_path: Path,
    token_id:  str | None = None,
    token_secret: str | None = None,
) -> None:
    """Send one page + mask to a Modal GPU deployment for LaMa inpainting."""
    import base64, modal
    global _modal_inpaint_fn

    if not mask_path.exists():
        shutil.copy2(page_path, out_path)
        return

    img_bytes = page_path.read_bytes()
    mask_b64  = base64.b64encode(mask_path.read_bytes()).decode()

    try:
        if token_id and token_secret:
            # Per-job BYOK: inject credentials temporarily under a lock.
            async with _get_modal_env_lock():
                old_id  = _os.environ.get("MODAL_TOKEN_ID")
                old_sec = _os.environ.get("MODAL_TOKEN_SECRET")
                try:
                    _os.environ["MODAL_TOKEN_ID"]     = token_id
                    _os.environ["MODAL_TOKEN_SECRET"] = token_secret
                    fn = modal.Function.from_name("hebrew-manga-translator", "inpaint_page")
                    result_bytes = await asyncio.to_thread(fn.remote, img_bytes, mask_b64)
                finally:
                    if old_id  is not None: _os.environ["MODAL_TOKEN_ID"]     = old_id
                    elif "MODAL_TOKEN_ID"     in _os.environ: del _os.environ["MODAL_TOKEN_ID"]
                    if old_sec is not None: _os.environ["MODAL_TOKEN_SECRET"] = old_sec
                    elif "MODAL_TOKEN_SECRET" in _os.environ: del _os.environ["MODAL_TOKEN_SECRET"]
        else:
            # Server-level Modal (USE_MODAL=true in .env) — cache the function handle.
            if _modal_inpaint_fn is None:
                _modal_inpaint_fn = modal.Function.from_name("hebrew-manga-translator", "inpaint_page")
            result_bytes = await asyncio.to_thread(_modal_inpaint_fn.remote, img_bytes, mask_b64)
    except Exception as exc:
        if _is_modal_auth_error(exc):
            raise RuntimeError(
                "Modal authentication failed — your Token ID or Token Secret is invalid. "
                "Go to Settings and re-enter your Modal tokens."
            ) from exc
        raise

    out_path.write_bytes(result_bytes)


# ---------------------------------------------------------------------------
# Synchronous per-page worker (runs inside the single-thread executor)
# ---------------------------------------------------------------------------

def _inpaint_page(page_path: Path, mask_path: Path, out_path: Path) -> None:
    """
    Full pipeline for a single page:
      1. Load original image + mask
      2. Fast-path: copy original if mask is empty (no text found)
      3. Try LaMa → fall back to OpenCV on any failure
      4. Save result as lossless PNG
    """

    # ── Load original ────────────────────────────────────────────────────────
    image_pil = Image.open(page_path).convert("RGB")

    # ── Load mask ────────────────────────────────────────────────────────────
    if not mask_path.exists():
        # Detector produced no mask → no text on this page → pass through
        shutil.copy2(page_path, out_path)
        return

    mask_pil = Image.open(mask_path).convert("L")
    mask_np  = np.array(mask_pil, dtype=np.uint8)

    # ── Refine mask: zero-out regions where OCR found no text ────────────────
    # If a speech bubble was detected but OCR couldn't read it, erasing it
    # would leave a blank white bubble with no translation.  Keep the original
    # artwork pixels in those regions so the source text stays visible instead.
    json_path = page_path.parent.parent / "detection" / f"{page_path.stem}.json"
    if json_path.exists():
        try:
            page_data = _json.loads(json_path.read_text(encoding="utf-8"))
            for region in page_data.get("regions", []):
                if not region.get("source_text"):
                    bbox = region.get("bbox", [])
                    if len(bbox) == 4:
                        x1, y1, x2, y2 = (int(v) for v in bbox)
                        mask_np[y1:y2, x1:x2] = 0
        except Exception:
            pass  # if JSON is unreadable, fall back to original mask

    # ── Fast-path: empty mask ────────────────────────────────────────────────
    if mask_np.max() == 0:
        # No active pixels → nothing to inpaint → copy original unchanged
        shutil.copy2(page_path, out_path)
        return

    # Rebuild PIL mask from the (possibly refined) numpy array
    mask_pil = Image.fromarray(mask_np)

    # ── Choose backend ───────────────────────────────────────────────────────
    lama = _get_lama()

    if lama is not None:
        try:
            result_pil = _lama_inpaint(lama, image_pil, mask_pil)
        except Exception as exc:
            print(f"[inpainter] LaMa inference error on {page_path.name} "
                  f"({exc}), switching to OpenCV for this page.")
            result_pil = _cv2_inpaint(image_pil, mask_np)
    else:
        result_pil = _cv2_inpaint(image_pil, mask_np)

    # ── Save ─────────────────────────────────────────────────────────────────
    result_pil.save(str(out_path), format="PNG")


# ---------------------------------------------------------------------------
# Backend: LaMa (primary)
# ---------------------------------------------------------------------------

def _lama_inpaint(
    lama,
    image_pil: Image.Image,
    mask_pil: Image.Image,
) -> Image.Image:
    """
    Neural inpainting with LaMa (Large Mask Inpainting).

    LaMa pads images internally to a multiple of its receptive-field stride,
    then crops back — output should match input size, but we enforce it
    explicitly to guard against any library version quirks.
    """
    original_size = image_pil.size  # (width, height)

    result = lama(image_pil, mask_pil)
    result = result.convert("RGB")

    # Size guard: if LaMa returned a different size (shouldn't happen, but
    # defensive), resize back so downstream steps get consistent dimensions.
    if result.size != original_size:
        result = result.resize(original_size, Image.LANCZOS)

    return result


# ---------------------------------------------------------------------------
# Backend: OpenCV TELEA (fallback)
# ---------------------------------------------------------------------------

def _cv2_inpaint(
    image_pil: Image.Image,
    mask_np: np.ndarray,
) -> Image.Image:
    """
    Fast inpainting using the TELEA algorithm (fast marching method).

    Ideal for the uniform white or lightly-textured backgrounds found inside
    most manga speech bubbles. Less accurate than LaMa on complex artwork
    (screen tones, dense crosshatching) but always available and very fast.

    OpenCV expects BGR, so we convert in/out.
    """
    img_bgr    = cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)
    result_bgr = cv2.inpaint(img_bgr, mask_np, _CV2_RADIUS, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB))
