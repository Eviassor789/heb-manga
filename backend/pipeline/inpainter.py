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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from core.job_manager import EmitFn

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

async def inpaint(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Erase text regions from every page using the masks from Step 1.

    Writes cleaned images to <job_dir>/cleaned/.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "inpaint", "status": "running"})

    detection_dir = job_dir / "detection"
    cleaned_dir   = job_dir / "cleaned"
    loop  = asyncio.get_running_loop()
    total = len(pages)

    for i, page_path in enumerate(pages, start=1):
        mask_path = detection_dir / f"{page_path.stem}_mask.png"
        out_path  = cleaned_dir   / page_path.name   # same filename, different dir

        await loop.run_in_executor(
            _executor, _inpaint_page, page_path, mask_path, out_path
        )
        await emit({"stage": "inpaint", "status": "running", "page": i, "total": total})

    await emit({"stage": "inpaint", "status": "done", "total_pages": total})
    return pages


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

    # ── Fast-path: empty mask ────────────────────────────────────────────────
    if mask_np.max() == 0:
        # No active pixels → nothing to inpaint → copy original unchanged
        shutil.copy2(page_path, out_path)
        return

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
