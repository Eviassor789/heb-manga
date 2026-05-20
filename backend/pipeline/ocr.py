"""
Step 2 — OCR (Text Extraction)

For each page:
  1. Read the detection JSON from Step 1  (<job_dir>/detection/NNN.json)
  2. For every dialogue / narration region, crop that area from the original image
  3. Pre-process the crop (padding + upscale) for better EasyOCR accuracy
  4. Run EasyOCR, sort detected lines top-to-bottom, join into one string
  5. Write "source_text" back into the same JSON  (in-place update)

SFX regions (type == "sfx") are skipped intentionally — they are not
translated in the MVP.

The JSON schema after this step:
  region = {
    "id": 0,
    "bbox": [x1, y1, x2, y2],
    "polygon": [...],
    "type": "dialogue",
    "vertical": false,
    "confidence": 0.95,
    "source_text": "I never said that!",   ← filled here
    "hebrew_text": null                     ← filled by Step 4
  }
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from utils.job_manager import EmitFn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# EasyOCR downloads its own models (~100 MB) here on first use
_EASYOCR_MODEL_DIR = _BACKEND_DIR / "models" / "easyocr"

# Confidence below this → discard the OCR result for that line
_CONFIDENCE_THRESHOLD = 0.30

# Crops smaller than this in any dimension get upscaled before OCR
_MIN_CROP_DIM = 48

# White border added around every crop (pixels)
# Text flush against the edge degrades EasyOCR's line-detection accuracy
_CROP_PADDING = 10

# ---------------------------------------------------------------------------
# Lazy singleton reader (thread-safe double-checked locking)
# ---------------------------------------------------------------------------

_reader = None
_reader_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def _get_reader():
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                import easyocr  # noqa: PLC0415  (deferred import — model loads here)

                _EASYOCR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
                _reader = easyocr.Reader(
                    lang_list=["en"],
                    gpu=False,
                    model_storage_directory=str(_EASYOCR_MODEL_DIR),
                    download_enabled=True,   # auto-downloads on first run
                    verbose=False,
                )
    return _reader


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def ocr(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Run OCR on all detected dialogue regions across every page.

    Updates each detection/NNN.json in-place with source_text values.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "ocr", "status": "running"})
    detection_dir = job_dir / "detection"
    loop = asyncio.get_running_loop()
    total = len(pages)

    for i, page_path in enumerate(pages, start=1):
        json_path = detection_dir / f"{page_path.stem}.json"

        if not json_path.exists():
            # Detection produced no output for this page (no text found) — skip
            await emit({"stage": "ocr", "status": "running", "page": i, "total": total})
            continue

        await loop.run_in_executor(_executor, _ocr_page, page_path, json_path)
        await emit({"stage": "ocr", "status": "running", "page": i, "total": total})

    await emit({"stage": "ocr", "status": "done", "total_pages": total})
    return pages


# ---------------------------------------------------------------------------
# Synchronous per-page OCR (runs inside thread pool)
# ---------------------------------------------------------------------------

def _ocr_page(page_path: Path, json_path: Path) -> None:
    reader = _get_reader()

    page_data = json.loads(json_path.read_text(encoding="utf-8"))

    img_bgr = cv2.imread(str(page_path))
    if img_bgr is None:
        raise ValueError(f"Could not read image: {page_path}")

    # EasyOCR expects RGB
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img_h, img_w = img_rgb.shape[:2]

    for region in page_data["regions"]:
        if region.get("type") == "sfx":
            continue  # sound effects are not OCR'd or translated at MVP

        x1, y1, x2, y2 = region["bbox"]

        # Clamp to image boundaries (detector may produce slightly OOB boxes)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img_w, x2), min(img_h, y2)

        if x2 <= x1 or y2 <= y1:
            continue  # degenerate box after clamping

        crop = img_rgb[y1:y2, x1:x2]
        crop = _preprocess(crop)
        text = _extract_text(reader, crop)
        region["source_text"] = text if text else None

    json_path.write_text(
        json.dumps(page_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def _preprocess(crop: np.ndarray) -> np.ndarray:
    """
    Prepare a bounding-box crop for EasyOCR:

    1. Add white padding — text touching the edge confuses EasyOCR's internal
       text-region detector, causing missed lines.
    2. Upscale tiny crops — EasyOCR accuracy degrades below ~48 px in height.
       Scaling to a minimum dimension preserves aspect ratio.
    """
    # Step 1: white border
    crop = cv2.copyMakeBorder(
        crop,
        _CROP_PADDING, _CROP_PADDING, _CROP_PADDING, _CROP_PADDING,
        cv2.BORDER_CONSTANT,
        value=(255, 255, 255),
    )

    # Step 2: upscale if too small
    h, w = crop.shape[:2]
    min_dim = min(h, w)
    if min_dim < _MIN_CROP_DIM and min_dim > 0:
        scale = _MIN_CROP_DIM / min_dim
        new_w = max(1, round(w * scale))
        new_h = max(1, round(h * scale))
        crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    return crop


# ---------------------------------------------------------------------------
# OCR + text assembly
# ---------------------------------------------------------------------------

def _extract_text(reader, crop: np.ndarray) -> str:
    """
    Run EasyOCR on a pre-processed crop and return clean extracted text.

    EasyOCR returns one result per detected text line:
        (bbox_points, text, confidence)
    where bbox_points = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]

    Strategy:
    • Filter out low-confidence results.
    • Sort remaining lines top-to-bottom (then left-to-right for ties).
    • Join with a single space — the translator will handle line-break intent.
    • Run through _sanitize() to remove OCR noise common in comic fonts.
    """
    try:
        results = reader.readtext(crop, detail=1, paragraph=False)
    except Exception:
        return ""

    if not results:
        return ""

    # Filter by confidence
    filtered = [
        (bbox, text.strip(), conf)
        for bbox, text, conf in results
        if conf >= _CONFIDENCE_THRESHOLD and text.strip()
    ]
    if not filtered:
        return ""

    # Sort: primary = top-left y (bbox[0][1]), secondary = top-left x (bbox[0][0])
    filtered.sort(key=lambda r: (r[0][0][1], r[0][0][0]))

    raw_text = " ".join(text for _, text, _ in filtered)
    return _sanitize(raw_text)


def _sanitize(text: str) -> str:
    """
    Remove common OCR artefacts produced by comic-style fonts:

    • Collapse runs of whitespace into a single space.
    • Discard the whole result if it's a single non-alphabetic character
      (almost always a misread ink smudge or dot).
    • Strip leading/trailing punctuation that appears without adjacent words
      (e.g. a lone "-" or "." at the start).
    """
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Single-char garbage
    if len(text) == 1 and not text.isalpha():
        return ""

    # Leading noise: strip a lone punctuation char followed by a space
    text = re.sub(r"^[^A-Za-z0-9\"\'(]+\s+", "", text)

    return text.strip()
