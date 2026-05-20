"""
Step 1 — Text Detection

Runs comic-text-detector (YOLO-based, trained on comic speech bubbles) on each
page image to produce:

  <job_dir>/detection/NNN.json        region metadata (bbox, type, confidence)
  <job_dir>/detection/NNN_mask.png    binary text mask consumed by Step 3 (inpainter)

Design notes
────────────
• The detector model is loaded lazily on the first call and cached as a
  module-level singleton. Subsequent pages pay zero load overhead.
• All blocking work (model load + OpenCV inference) runs in a single-thread
  ThreadPoolExecutor so it never stalls the FastAPI event loop.
• comic-text-detector is not on PyPI. It must be cloned into backend/vendor/:
    cd backend
    git clone https://github.com/dmMaze/comic-text-detector vendor/comic-text-detector
  Then download weights: python utils/download_models.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from utils.job_manager import EmitFn

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_VENDOR_DIR = _BACKEND_DIR / "vendor" / "comic-text-detector"
_DEFAULT_MODEL = _BACKEND_DIR / "models" / "comic_text_detector" / "comictextdetector.pt"

# ---------------------------------------------------------------------------
# Singleton detector (thread-safe double-checked locking)
# ---------------------------------------------------------------------------

_detector = None
_detector_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detector")


def _ensure_vendor_on_path() -> None:
    if not _VENDOR_DIR.exists():
        raise RuntimeError(
            "comic-text-detector source not found.\n"
            f"  Expected: {_VENDOR_DIR}\n"
            "  Fix: cd backend && "
            "git clone https://github.com/dmMaze/comic-text-detector vendor/comic-text-detector"
        )
    if str(_VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(_VENDOR_DIR))


def _load_detector():
    _ensure_vendor_on_path()

    # Imported here because the module only exists after the git clone
    from inference import TextDetector  # noqa: PLC0415  (comic-text-detector/inference.py)

    model_path = Path(os.getenv("DETECTOR_MODEL_PATH", str(_DEFAULT_MODEL)))
    if not model_path.exists():
        raise RuntimeError(
            f"Detector weights not found: {model_path}\n"
            "  Fix: python utils/download_models.py"
        )

    return TextDetector(
        model_path=str(model_path),
        input_size=1024,
        device="cpu",
        act="leaky",
    )


def _get_detector():
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                _detector = _load_detector()
    return _detector


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def detect(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Detect text regions across all pages.

    Writes per-page JSON + mask PNGs into <job_dir>/detection/.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "detect", "status": "running"})
    detection_dir = job_dir / "detection"
    loop = asyncio.get_running_loop()
    total = len(pages)

    for i, page_path in enumerate(pages, start=1):
        await loop.run_in_executor(_executor, _detect_page, page_path, detection_dir)
        await emit({"stage": "detect", "status": "running", "page": i, "total": total})

    await emit({"stage": "detect", "status": "done", "total_pages": total})
    return pages


# ---------------------------------------------------------------------------
# Synchronous per-page detection (runs inside thread pool)
# ---------------------------------------------------------------------------

def _detect_page(page_path: Path, detection_dir: Path) -> None:
    detector = _get_detector()

    img_bgr = cv2.imread(str(page_path))
    if img_bgr is None:
        raise ValueError(f"cv2 could not read image: {page_path}")
    h, w = img_bgr.shape[:2]

    blk_list, mask = detector.detect(img_bgr)

    # ── Mask ────────────────────────────────────────────────────────────────
    # mask is a uint8 ndarray: 255 = text region, 0 = background.
    # A small morphological dilation ensures stray pixels around letter edges
    # are fully covered, which improves LaMa inpainting quality.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask_dilated = cv2.dilate(mask, kernel, iterations=2)

    mask_path = detection_dir / f"{page_path.stem}_mask.png"
    cv2.imwrite(str(mask_path), mask_dilated)

    # ── Regions JSON ────────────────────────────────────────────────────────
    regions: list[dict] = []
    for idx, blk in enumerate(blk_list):
        bbox = _extract_bbox(blk)
        if bbox is None:
            continue  # skip degenerate boxes

        regions.append({
            "id": idx,
            "bbox": bbox,                         # [x1, y1, x2, y2]
            "polygon": _bbox_as_polygon(bbox),    # four-corner rectangle
            "type": _classify_type(blk),          # "dialogue" | "sfx"
            "vertical": bool(getattr(blk, "vertical", False)),
            "confidence": round(float(getattr(blk, "prob", 1.0)), 4),
            "source_text": None,  # populated by Step 2 — ocr.py
            "hebrew_text": None,  # populated by Step 4 — translator.py
        })

    page_data = {
        "page": page_path.name,
        "image_size": {"width": w, "height": h},
        "mask": mask_path.name,
        "regions": regions,
    }

    json_path = detection_dir / f"{page_path.stem}.json"
    json_path.write_text(
        json.dumps(page_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_bbox(blk) -> list[int] | None:
    """
    Safely pull [x1, y1, x2, y2] from a TextBlock.
    Returns None for degenerate or missing boxes.
    """
    try:
        arr = np.asarray(blk.xyxy).flatten()
        if arr.size < 4:
            return None
        x1, y1, x2, y2 = (int(v) for v in arr[:4])
        if x2 <= x1 or y2 <= y1:
            return None
        return [x1, y1, x2, y2]
    except Exception:
        return None


def _bbox_as_polygon(bbox: list[int]) -> list[list[int]]:
    """[x1, y1, x2, y2] → four-corner [[x, y], …] polygon."""
    x1, y1, x2, y2 = bbox
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _classify_type(blk) -> str:
    """
    Heuristic type label.

    comic-text-detector does not expose a semantic type field, so we infer:
      vertical=True  →  sfx   (upright SFX glyphs; rarely seen in English comics)
      vertical=False →  dialogue

    Narration-box vs. speech-bubble distinction is a v2 classifier feature.
    SFX regions are detected but skipped by the translator (Step 4).
    """
    return "sfx" if getattr(blk, "vertical", False) else "dialogue"
