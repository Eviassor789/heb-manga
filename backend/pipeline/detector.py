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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from core.job_manager import EmitFn

# ---------------------------------------------------------------------------
# Paths + Modal flag
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_VENDOR_DIR = _BACKEND_DIR / "vendor" / "comic-text-detector"
_DEFAULT_MODEL = _BACKEND_DIR / "models" / "comic_text_detector" / "comictextdetector.pt"

# Set USE_MODAL=true in backend/.env to offload detection to Modal GPU.
# Requires:  pip install modal  +  modal token new  +  modal deploy modal_gpu.py
_USE_MODAL = os.getenv("USE_MODAL", "false").lower() == "true"


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
    If USE_MODAL=true, each page is sent to a Modal T4 GPU instead of running locally.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "detect", "status": "running"})

    detection_dir = job_dir / "detection"
    loop  = asyncio.get_running_loop()
    total = len(pages)
    modal_seconds = 0.0

    for i, page_path in enumerate(pages, start=1):
        if _USE_MODAL:
            t0 = time.perf_counter()
            await _detect_page_modal(page_path, detection_dir)
            modal_seconds += time.perf_counter() - t0
        else:
            await loop.run_in_executor(_executor, _detect_page, page_path, detection_dir)
        await emit({"stage": "detect", "status": "running", "page": i, "total": total})

    await emit({
        "stage": "detect", "status": "done", "total_pages": total,
        "modal_gpu_seconds": round(modal_seconds, 2),
    })
    return pages


_modal_detect_fn = None   # cached after first lookup


async def _detect_page_modal(page_path: Path, detection_dir: Path) -> None:
    """Send one page to the server's Modal GPU deployment and write results to detection/."""
    import base64, json as _json, modal
    global _modal_detect_fn

    if _modal_detect_fn is None:
        _modal_detect_fn = modal.Function.from_name("hebrew-manga-translator", "detect_page")

    img_bytes = page_path.read_bytes()
    result    = await asyncio.to_thread(_modal_detect_fn.remote, img_bytes)

    # ── Write mask ────────────────────────────────────────────────────────────
    mask_path = detection_dir / f"{page_path.stem}_mask.png"
    mask_path.write_bytes(base64.b64decode(result["mask_b64"]))

    # ── Write detection JSON ──────────────────────────────────────────────────
    deduped_regions = nms_regions(result["regions"])
    page_data = {
        "page":       page_path.name,
        "image_size": result["image_size"],
        "mask":       mask_path.name,
        "regions":    deduped_regions,
    }
    (detection_dir / f"{page_path.stem}.json").write_text(
        _json.dumps(page_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Synchronous per-page detection (runs inside thread pool)
# ---------------------------------------------------------------------------

def _detect_page(page_path: Path, detection_dir: Path) -> None:
    detector = _get_detector()

    img_bgr = cv2.imread(str(page_path))
    if img_bgr is None:
        raise ValueError(f"cv2 could not read image: {page_path}")
    h, w = img_bgr.shape[:2]

    # TextDetector.__call__ returns (mask_raw, mask_refined, blk_list).
    # mask_refined already has text-region contours cleaned up by the model;
    # we use it as our base and apply a small dilation to ensure edge pixels
    # are fully covered for LaMa inpainting.
    mask_raw, mask_refined, blk_list = detector(img_bgr)

    # ── Mask ────────────────────────────────────────────────────────────────
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask_dilated = cv2.dilate(mask_refined, kernel, iterations=2)

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

    # Deduplicate: the model sometimes fires twice on the same balloon
    regions = nms_regions(regions)

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
        # blk.xyxy is [x1, y1, x2, y2] (list of ints set in TextBlock.__init__)
        x1, y1, x2, y2 = (int(v) for v in blk.xyxy[:4])
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


# ---------------------------------------------------------------------------
# Non-Maximum Suppression — deduplicates overlapping detections
# ---------------------------------------------------------------------------

_NMS_IOU_THRESHOLD = 0.45   # boxes with IoU above this are considered duplicates


def _iou(a: list[int], b: list[int]) -> float:
    """Intersection-over-Union for two [x1, y1, x2, y2] bounding boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union  = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _containment(small: list[int], large: list[int]) -> float:
    """Fraction of `small` that is covered by `large` (0–1)."""
    ix1, iy1 = max(small[0], large[0]), max(small[1], large[1])
    ix2, iy2 = min(small[2], large[2]), min(small[3], large[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    a = _area(small)
    return inter / a if a > 0 else 0.0


def nms_regions(regions: list[dict], iou_threshold: float = _NMS_IOU_THRESHOLD) -> list[dict]:
    """
    Remove near-duplicate regions produced by the detector.

    Handles three distinct cases:

    Case A — same bubble detected twice (IoU ≈ 0.95, boxes nearly identical):
        Caught by the IoU threshold (0.45).

    Case B — connected/adjacent bubbles where the detector fires one large
        merged box covering both AND smaller individual boxes for each one.
        Caught by the first pass: the merged box is identified as a "merger"
        (it substantially contains ≥ 2 smaller candidates) and discarded in
        favour of the more precise individual boxes.

    Case C — connected/adjacent bubbles where the detector ONLY fires the
        merged box (no individual sub-boxes).  We cannot split it further here,
        so it is kept as-is.  The typesetter will render the combined text in
        the merged bbox — imperfect but not worse than before.

    Sort order: (confidence DESC, area DESC) so higher-confidence / larger
    boxes are considered first in the second NMS pass.
    """
    sorted_r = sorted(
        regions,
        key=lambda r: (r.get("confidence", 0.0), _area(r["bbox"])),
        reverse=True,
    )
    n = len(sorted_r)

    # ── Pass 1: identify "merger" boxes ───────────────────────────────────────
    # A box is a merger if it substantially contains 2+ meaningfully smaller
    # candidates.  When we have both the merged box and the individual boxes,
    # we prefer the individual ones (finer granularity = better text placement).
    merger_indices: set[int] = set()
    for i in range(n):
        bbox_i  = sorted_r[i]["bbox"]
        area_i  = _area(bbox_i)
        if area_i == 0:
            continue
        # Count candidates that are (a) significantly smaller and
        # (b) mostly inside this box.
        children = [
            j for j in range(n)
            if j != i
            and _area(sorted_r[j]["bbox"]) < area_i * 0.85     # meaningfully smaller
            and _containment(sorted_r[j]["bbox"], bbox_i) > 0.75  # mostly inside
        ]
        if len(children) >= 2:
            merger_indices.add(i)

    # ── Pass 2: standard NMS skipping identified merger boxes ─────────────────
    kept: list[dict] = []
    for i, candidate in enumerate(sorted_r):
        if i in merger_indices:
            continue  # prefer the individual children detected for this region

        bbox_c = candidate["bbox"]
        suppress = False
        for k in kept:
            bbox_k = k["bbox"]
            if _iou(bbox_c, bbox_k) > iou_threshold:
                suppress = True
                break
            # Containment: candidate is mostly inside a larger kept region
            if _containment(bbox_c, bbox_k) > 0.75:
                suppress = True
                break
        if not suppress:
            kept.append(candidate)

    # Re-assign contiguous IDs so downstream steps can use them as list indices
    for new_id, region in enumerate(kept):
        region["id"] = new_id

    return kept
