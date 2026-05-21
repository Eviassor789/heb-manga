"""
Step 2 — OCR (Text Extraction)

Uses Gemini Vision by default: all bubble crops for a page are sent in a
single multipart API call, giving Gemini the full set of crops with their IDs.

Why Gemini Vision instead of EasyOCR
──────────────────────────────────────
EasyOCR was trained on clean document text. Comic/manga fonts are all-caps,
heavily stylised, sometimes hand-lettered, and tightly kerned inside irregular
shapes. EasyOCR routinely drops words or misreads entire lines.

Gemini Vision handles these fonts natively and almost never misses a word.
Sending all crops in one request keeps cost near zero (~$0.0002/page extra)
and gets all region texts in a single round-trip.

Fallback
────────
If GEMINI_API_KEY is missing or the call fails after retries, the module
falls back to local EasyOCR so the pipeline never crashes.  Set
USE_GEMINI_OCR=false to always use EasyOCR (useful for offline testing).

Note on Modal
─────────────
OCR no longer uses the Modal GPU path — Gemini Vision doesn't need a GPU.
USE_MODAL still controls detect and inpaint; it has no effect here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from core.job_manager import EmitFn
from core.rate_limiter import call_with_backoff

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_EASYOCR_MODEL_DIR = _BACKEND_DIR / "models" / "easyocr"

_USE_GEMINI_OCR = os.getenv("USE_GEMINI_OCR", "true").lower() == "true"
_DEFAULT_MODEL  = "gemini-2.5-flash"

# Gemini 2.5 Flash pricing — same rates as translator.py
_PRICE_INPUT_PER_M  = 0.075
_PRICE_OUTPUT_PER_M = 0.300
_PRICE_THINK_PER_M  = 0.300   # thinking = output rate for 2.5 Flash
_ILS_PER_USD        = 3.65

# Crop padding (pixels added around each bbox before sending to Gemini/EasyOCR)
_CROP_PAD = 8

# ---------------------------------------------------------------------------
# Gemini client singleton (shared with translator but created independently
# here so ocr.py has no cross-module dependency)
# ---------------------------------------------------------------------------

_ocr_client     = None
_ocr_client_lock = threading.Lock()


def _get_ocr_client():
    global _ocr_client
    if _ocr_client is None:
        with _ocr_client_lock:
            if _ocr_client is None:
                from google import genai  # noqa: PLC0415
                api_key = os.getenv("GEMINI_API_KEY", "").strip()
                if api_key:
                    _ocr_client = genai.Client(api_key=api_key)
    return _ocr_client


# ---------------------------------------------------------------------------
# EasyOCR fallback singleton
# ---------------------------------------------------------------------------

_reader      = None
_reader_lock = threading.Lock()
_executor    = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")

_CONFIDENCE_THRESHOLD = 0.30
_MIN_CROP_DIM         = 48
_EASYOCR_CROP_PADDING = 10


def _get_reader():
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                import easyocr  # noqa: PLC0415
                _EASYOCR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
                _reader = easyocr.Reader(
                    lang_list=["en"],
                    gpu=False,
                    model_storage_directory=str(_EASYOCR_MODEL_DIR),
                    download_enabled=True,
                    verbose=False,
                )
    return _reader


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def ocr(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Extract text from all detected dialogue regions across every page.

    Default path  : Gemini Vision — one multipart call per page (all crops)
    Fallback path : EasyOCR       — USE_GEMINI_OCR=false or Gemini unavailable

    Updates each detection/NNN.json in-place with source_text values.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "ocr", "status": "running"})

    detection_dir = job_dir / "detection"
    loop  = asyncio.get_running_loop()
    total = len(pages)

    # Token accounting (Gemini Vision calls)
    tok_input = tok_output = tok_think = 0

    for i, page_path in enumerate(pages, start=1):
        json_path = detection_dir / f"{page_path.stem}.json"

        if not json_path.exists():
            await emit({"stage": "ocr", "status": "running", "page": i, "total": total})
            continue

        if _USE_GEMINI_OCR and _get_ocr_client() is not None:
            page_tokens = await _ocr_page_gemini(page_path, json_path)
            tok_input  += page_tokens.get("input",  0)
            tok_output += page_tokens.get("output", 0)
            tok_think  += page_tokens.get("think",  0)
        else:
            await loop.run_in_executor(_executor, _ocr_page_easyocr, page_path, json_path)

        await emit({"stage": "ocr", "status": "running", "page": i, "total": total})

    # Cost summary (only populated when Gemini Vision OCR was used)
    ocr_cost: dict | None = None
    if tok_input + tok_output + tok_think > 0:
        cost_usd = (
            tok_input  / 1_000_000 * _PRICE_INPUT_PER_M +
            tok_output / 1_000_000 * _PRICE_OUTPUT_PER_M +
            tok_think  / 1_000_000 * _PRICE_THINK_PER_M
        )
        ocr_cost = {
            "usd":    round(cost_usd, 4),
            "ils":    round(cost_usd * _ILS_PER_USD, 4),
            "tokens": {
                "input":  tok_input,
                "output": tok_output,
                "think":  tok_think,
                "total":  tok_input + tok_output + tok_think,
            },
        }

    await emit({
        "stage":             "ocr",
        "status":            "done",
        "total_pages":       total,
        "modal_gpu_seconds": 0,          # OCR no longer uses Modal GPU
        **({"cost": ocr_cost} if ocr_cost else {}),
    })
    return pages


# ---------------------------------------------------------------------------
# Gemini Vision OCR  (primary path)
# ---------------------------------------------------------------------------

def _extract_ocr_tokens(response) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return {"input": 0, "output": 0, "think": 0}
    return {
        "input":  int(getattr(usage, "prompt_token_count",     0) or 0),
        "output": int(getattr(usage, "candidates_token_count", 0) or 0),
        "think":  int(getattr(usage, "thoughts_token_count",   0) or 0),
    }


async def _ocr_page_gemini(page_path: Path, json_path: Path) -> dict[str, int]:
    """
    Send all bubble crops for one page to Gemini Vision in a single call.

    Returns accumulated token counts so the caller can compute cost.
    Each crop is sent as a separate inline image part, preceded by a text
    label "Region N:" so Gemini knows which ID to assign to each result.
    Temperature=0 forces the most deterministic reading.
    """
    from google.genai import types  # noqa: PLC0415

    page_data = json.loads(json_path.read_text(encoding="utf-8"))
    to_ocr = [
        r for r in page_data["regions"]
        if r.get("type") != "sfx" and not r.get("source_text")
    ]
    if not to_ocr:
        return {"input": 0, "output": 0, "think": 0}

    img_bgr = cv2.imread(str(page_path))
    if img_bgr is None:
        log.warning("[ocr] Could not read image: %s", page_path)
        return {"input": 0, "output": 0, "think": 0}
    h, w = img_bgr.shape[:2]

    # ── Build multipart content ────────────────────────────────────────────
    parts: list = []
    valid_ids: list[int] = []

    for region in to_ocr:
        x1, y1, x2, y2 = region["bbox"]
        x1 = max(0, x1 - _CROP_PAD);  y1 = max(0, y1 - _CROP_PAD)
        x2 = min(w, x2 + _CROP_PAD);  y2 = min(h, y2 + _CROP_PAD)
        if x2 <= x1 or y2 <= y1:
            continue

        crop = img_bgr[y1:y2, x1:x2]
        _, buf = cv2.imencode(
            ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92]
        )

        parts.append(types.Part(text=f"Region {region['id']}:"))
        parts.append(types.Part.from_bytes(
            data=buf.tobytes(), mime_type="image/jpeg"
        ))
        valid_ids.append(region["id"])

    if not valid_ids:
        return {"input": 0, "output": 0, "think": 0}

    parts.append(types.Part(text=(
        "Read the English text from each numbered manga speech-bubble crop above.\n"
        "Rules:\n"
        "• Copy EVERY word exactly as it appears — do not skip, fix, or paraphrase anything.\n"
        "• Preserve ALL CAPS, punctuation, and line breaks merged into a single string.\n"
        "• If a crop has no readable text, use null.\n\n"
        "Return ONLY valid JSON — no markdown, no explanation:\n"
        '{"regions": [{"id": <int>, "source_text": "<text or null>"}]}\n\n'
        f"Required region IDs: {valid_ids}"
    )))

    client = _get_ocr_client()
    model  = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0,
    )
    total_tokens = {"input": 0, "output": 0, "think": 0}

    try:
        response = await call_with_backoff(
            lambda: client.aio.models.generate_content(
                model=model,
                contents=parts,
                config=config,
            )
        )
        for k, v in _extract_ocr_tokens(response).items():
            total_tokens[k] += v

        raw  = _strip_md(response.text)
        data = json.loads(raw)

        id_to_text: dict[int, str | None] = {}
        for item in data.get("regions", []):
            try:
                rid  = int(item["id"])
                text = (item.get("source_text") or "").strip() or None
                id_to_text[rid] = text
            except (KeyError, ValueError):
                continue

        # Retry any IDs that came back empty
        missing = [rid for rid in valid_ids if rid not in id_to_text]
        if missing:
            log.warning("[ocr] Gemini Vision missed IDs %s on %s — retrying",
                        missing, page_path.name)
            retry_texts, retry_tokens = await _gemini_retry(
                img_bgr, page_data["regions"], missing, model, config
            )
            id_to_text.update(retry_texts)
            for k, v in retry_tokens.items():
                total_tokens[k] += v

        for region in page_data["regions"]:
            if region["id"] in id_to_text:
                region["source_text"] = id_to_text[region["id"]]

        json_path.write_text(
            json.dumps(page_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("[ocr] Gemini Vision: %d region(s) on %s — tokens i=%d o=%d t=%d",
                 len(id_to_text), page_path.name,
                 total_tokens["input"], total_tokens["output"], total_tokens["think"])

    except Exception as exc:
        log.error("[ocr] Gemini Vision failed for %s (%s) — falling back to EasyOCR",
                  page_path.name, exc)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, _ocr_page_easyocr, page_path, json_path)

    return total_tokens


async def _gemini_retry(
    img_bgr:  np.ndarray,
    regions:  list[dict],
    missing:  list[int],
    model:    str,
    config,
) -> tuple[dict[int, str | None], dict[str, int]]:
    """Re-send only the missed crops in a second Gemini Vision call.

    Returns (id→text mapping, token counts) so the caller can accumulate both.
    """
    from google.genai import types  # noqa: PLC0415

    empty_tokens: dict[str, int] = {"input": 0, "output": 0, "think": 0}
    h, w  = img_bgr.shape[:2]
    parts: list = []
    valid_ids: list[int] = []

    reg_map = {r["id"]: r for r in regions}
    for rid in missing:
        region = reg_map.get(rid)
        if region is None:
            continue
        x1, y1, x2, y2 = region["bbox"]
        x1 = max(0, x1 - _CROP_PAD);  y1 = max(0, y1 - _CROP_PAD)
        x2 = min(w, x2 + _CROP_PAD);  y2 = min(h, y2 + _CROP_PAD)
        if x2 <= x1 or y2 <= y1:
            continue
        crop = img_bgr[y1:y2, x1:x2]
        _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        parts.append(types.Part(text=f"Region {rid}:"))
        parts.append(types.Part.from_bytes(data=buf.tobytes(), mime_type="image/jpeg"))
        valid_ids.append(rid)

    if not valid_ids:
        return {}, empty_tokens

    parts.append(types.Part(text=(
        "Read text from each region. Return JSON: "
        '{"regions": [{"id": N, "source_text": "..."}]}'
    )))

    client = _get_ocr_client()
    try:
        response = await call_with_backoff(
            lambda: client.aio.models.generate_content(
                model=model, contents=parts, config=config
            )
        )
        tokens = _extract_ocr_tokens(response)
        data = json.loads(_strip_md(response.text))
        texts = {
            int(item["id"]): (item.get("source_text") or "").strip() or None
            for item in data.get("regions", [])
            if "id" in item
        }
        return texts, tokens
    except Exception as exc:
        log.warning("[ocr] Retry also failed: %s", exc)
        return {}, empty_tokens


def _strip_md(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


# ---------------------------------------------------------------------------
# EasyOCR fallback  (runs in single-thread executor)
# ---------------------------------------------------------------------------

def _ocr_page_easyocr(page_path: Path, json_path: Path) -> None:
    reader    = _get_reader()
    page_data = json.loads(json_path.read_text(encoding="utf-8"))

    img_bgr = cv2.imread(str(page_path))
    if img_bgr is None:
        raise ValueError(f"Could not read image: {page_path}")

    img_rgb  = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img_h, img_w = img_rgb.shape[:2]

    for region in page_data["regions"]:
        if region.get("type") == "sfx":
            continue

        x1, y1, x2, y2 = region["bbox"]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img_w, x2), min(img_h, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        crop = img_rgb[y1:y2, x1:x2]
        crop = _easyocr_preprocess(crop)
        text = _easyocr_extract(reader, crop)
        region["source_text"] = text if text else None

    json_path.write_text(
        json.dumps(page_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _easyocr_preprocess(crop: np.ndarray) -> np.ndarray:
    crop = cv2.copyMakeBorder(
        crop,
        _EASYOCR_CROP_PADDING, _EASYOCR_CROP_PADDING,
        _EASYOCR_CROP_PADDING, _EASYOCR_CROP_PADDING,
        cv2.BORDER_CONSTANT, value=(255, 255, 255),
    )
    h, w    = crop.shape[:2]
    min_dim = min(h, w)
    if min_dim < _MIN_CROP_DIM and min_dim > 0:
        scale  = _MIN_CROP_DIM / min_dim
        new_w  = max(1, round(w * scale))
        new_h  = max(1, round(h * scale))
        crop   = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    return crop


def _easyocr_extract(reader, crop: np.ndarray) -> str:
    try:
        results = reader.readtext(crop, detail=1, paragraph=False)
    except Exception:
        return ""
    if not results:
        return ""

    filtered = [
        (bbox, text.strip(), conf)
        for bbox, text, conf in results
        if conf >= _CONFIDENCE_THRESHOLD and text.strip()
    ]
    if not filtered:
        return ""

    filtered.sort(key=lambda r: (r[0][0][1], r[0][0][0]))
    raw = " ".join(t for _, t, _ in filtered)
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) == 1 and not raw.isalpha():
        return ""
    raw = re.sub(r"^[^A-Za-z0-9\"\'(]+\s+", "", raw)
    return raw.strip()
