"""
Step 5 — Hebrew Typesetting  (final pipeline step)

For each page:
  1. Open the cleaned (text-erased) image from <job_dir>/cleaned/
  2. Read the detection JSON — every region now has a hebrew_text value
  3. For each translatable region, render the Hebrew text at the bounding
     box coordinates:
       • python-bidi  — Unicode BiDi algorithm → correct RTL visual order
       • Auto-shrink  — finds the largest font size that still fits the balloon
       • Centering    — horizontal + vertical centre inside the bounding box
  4. Save each typeset page to <job_dir>/output/NNN.png
  5. Assemble all pages into <job_dir>/output/result.pdf (via img2pdf)

Font
────
Priority order:
  1. HEBREW_FONT_PATH environment variable (absolute path to a .ttf/.otf)
  2. Any .ttf / .otf file found in backend/fonts/
  3. Auto-download Heebo-Bold.ttf from Google Fonts into backend/fonts/

RTL rendering note
──────────────────
Pillow draws strings left-to-right.  Hebrew *must* be passed through the
Unicode BiDi algorithm (python-bidi) before drawing, otherwise letters appear
in reversed order and the text is unreadable.

We apply get_display() per-line (not per-paragraph) and set base_dir='R'
explicitly so lines that start with a digit or a Latin character (e.g.
"5 חיילים") are still treated as right-to-left paragraphs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import img2pdf
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from utils.job_manager import EmitFn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_FONTS_DIR   = _BACKEND_DIR / "fonts"

_MAX_FONT_SIZE = 32    # largest font size to try (pixels)
_MIN_FONT_SIZE = 8     # never shrink below this
_LINE_SPACING  = 4     # extra vertical pixels between consecutive lines
_PADDING       = 8     # pixels between text block and bbox edges

_TEXT_COLOR = (0, 0, 0)   # solid black — speech bubbles have white/light bg

# Heebo Bold: clean, legible at small sizes, OFL license
_FONT_FILENAME    = "Heebo-Bold.ttf"
_FONT_DOWNLOAD_URL = (
    "https://github.com/google/fonts/raw/main/ofl/heebo/static/Heebo-Bold.ttf"
)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="typesetter")

# ---------------------------------------------------------------------------
# Font resolution (cached after first successful lookup)
# ---------------------------------------------------------------------------

_resolved_font_path: Path | None = None


def _resolve_font() -> Path:
    """
    Find or download the Hebrew TTF font, then cache the result.

    Raises RuntimeError if no font can be found or downloaded.
    """
    global _resolved_font_path

    if _resolved_font_path is not None and _resolved_font_path.exists():
        return _resolved_font_path

    # 1. Environment variable override
    env_val = os.getenv("HEBREW_FONT_PATH", "").strip()
    if env_val:
        p = Path(env_val)
        if p.exists():
            _resolved_font_path = p
            log.info("[typesetter] Using font from env: %s", p)
            return _resolved_font_path
        log.warning("[typesetter] HEBREW_FONT_PATH=%s not found, continuing search.", env_val)

    # 2. Scan backend/fonts/
    _FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        candidates = sorted(_FONTS_DIR.glob(pattern))
        if candidates:
            _resolved_font_path = candidates[0]
            log.info("[typesetter] Using font: %s", _resolved_font_path.name)
            return _resolved_font_path

    # 3. Auto-download Heebo-Bold.ttf
    dest = _FONTS_DIR / _FONT_FILENAME
    log.info("[typesetter] No font in fonts/ — downloading %s …", _FONT_FILENAME)
    try:
        urllib.request.urlretrieve(_FONT_DOWNLOAD_URL, str(dest))
        _resolved_font_path = dest
        log.info("[typesetter] Font saved → %s", dest)
        return _resolved_font_path
    except Exception as exc:
        raise RuntimeError(
            f"No Hebrew font available and auto-download failed: {exc}\n"
            f"  Fix: download any Hebrew TTF (e.g. Heebo-Bold.ttf) and place it in:\n"
            f"       {_FONTS_DIR}"
        ) from exc


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_resolve_font()), size)


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def typeset(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Render Hebrew text onto every page, then assemble output/result.pdf.

    Reads  : cleaned/NNN.png        (text-erased artwork from Step 3)
             detection/NNN.json     (hebrew_text per region from Step 4)
    Writes : output/NNN.png         (final typeset pages)
             output/result.pdf      (assembled PDF — served by download endpoint)

    Falls back to the original page if the cleaned version is missing.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "typeset", "status": "running"})

    cleaned_dir   = job_dir / "cleaned"
    detection_dir = job_dir / "detection"
    output_dir    = job_dir / "output"
    loop  = asyncio.get_running_loop()
    total = len(pages)

    for i, page_path in enumerate(pages, start=1):
        # Prefer cleaned (inpainted) image; fall back to original if missing
        src       = cleaned_dir / page_path.name
        if not src.exists():
            log.warning("[typesetter] cleaned/%s missing — using original.", page_path.name)
            src = page_path

        json_path = detection_dir / f"{page_path.stem}.json"
        out_path  = output_dir    / page_path.name

        await loop.run_in_executor(
            _executor, _typeset_page, src, json_path, out_path
        )
        await emit({"stage": "typeset", "status": "running", "page": i, "total": total})

    # Assemble all output PNGs → single PDF
    await loop.run_in_executor(_executor, _assemble_pdf, output_dir)
    await emit({"stage": "typeset", "status": "done", "total_pages": total})
    return pages


# ---------------------------------------------------------------------------
# Per-page typesetting (runs in single-thread executor)
# ---------------------------------------------------------------------------

def _typeset_page(src_path: Path, json_path: Path, out_path: Path) -> None:
    """
    Composite Hebrew text onto one page image and save to output/.

    If the detection JSON is missing (detector found no text), the cleaned
    image is simply copied across — still a valid output page.
    """
    img  = Image.open(src_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    if json_path.exists():
        page_data = json.loads(json_path.read_text(encoding="utf-8"))

        for region in page_data.get("regions", []):
            hebrew = (region.get("hebrew_text") or "").strip()
            if not hebrew:
                continue   # OCR or translation produced nothing
            if region.get("type") == "sfx":
                continue   # SFX not typeset at MVP

            _render_region(draw, hebrew, region["bbox"], img.size)

    img.save(str(out_path), format="PNG", optimize=False)


# ---------------------------------------------------------------------------
# Region renderer
# ---------------------------------------------------------------------------

def _render_region(
    draw:       ImageDraw.ImageDraw,
    hebrew:     str,
    bbox:       list[int],
    image_size: tuple[int, int],
) -> None:
    """
    Render one Hebrew text block centred inside its bounding box.

    Steps:
      1. Clamp bbox to image bounds
      2. Find the largest font size whose wrapped text fits (auto-shrink)
      3. Vertically centre the text block within the balloon
      4. Draw each BiDi-processed line horizontally centred
    """
    x1, y1, x2, y2 = bbox
    img_w, img_h   = image_size

    # Clamp
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img_w, x2), min(img_h, y2)
    if x2 <= x1 or y2 <= y1:
        return

    avail_w = x2 - x1 - 2 * _PADDING
    avail_h = y2 - y1 - 2 * _PADDING
    if avail_w < 4 or avail_h < 4:
        return

    font, lines = _fit_text(hebrew, avail_w, avail_h)
    if not lines:
        return

    # Line height from font metrics (consistent regardless of line content)
    ascent, descent = font.getmetrics()
    line_h  = ascent + descent + _LINE_SPACING
    total_h = len(lines) * line_h - _LINE_SPACING  # no trailing gap on last line

    # Vertical centre — clamp so we never draw above the top padding
    y_cursor  = y1 + _PADDING + max(0, (avail_h - total_h) // 2)
    x_centre  = (x1 + x2) // 2

    for line in lines:
        draw.text(
            (x_centre, y_cursor),
            line,
            font=font,
            fill=_TEXT_COLOR,
            # anchor "ma":
            #   m = horizontal middle (centres the line around x_centre)
            #   a = ascender top      (y_cursor is the top of the text, not baseline)
            anchor="ma",
        )
        y_cursor += line_h


# ---------------------------------------------------------------------------
# Text layout: auto-shrink + wrap + BiDi
# ---------------------------------------------------------------------------

def _fit_text(
    text:      str,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """
    Return (font, bidi_lines) for the largest font size that fits.

    Tries every integer size from _MAX_FONT_SIZE down to _MIN_FONT_SIZE.
    At _MIN_FONT_SIZE the text is returned regardless of height overflow
    (it will be clipped by the image boundary rather than silently dropped).
    """
    for size in range(_MAX_FONT_SIZE, _MIN_FONT_SIZE - 1, -1):
        try:
            font = _load_font(size)
        except Exception:
            continue

        lines = _wrap_and_bidi(text, font, max_width)
        if not lines:
            continue

        ascent, descent = font.getmetrics()
        line_h  = ascent + descent + _LINE_SPACING
        total_h = len(lines) * line_h - _LINE_SPACING

        fits = total_h <= max_height
        if fits or size == _MIN_FONT_SIZE:
            if not fits:
                log.warning(
                    "[typesetter] Text overflows bbox at min font %dpx: %r",
                    _MIN_FONT_SIZE,
                    text[:40],
                )
            return font, lines

    # Unreachable in practice, but satisfies type checker
    font = _load_font(_MIN_FONT_SIZE)
    return font, _wrap_and_bidi(text, font, max_width)


def _wrap_and_bidi(
    text:      str,
    font:      ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """
    Word-wrap Hebrew text and apply the Unicode BiDi algorithm per line.

    Wrapping strategy
    ─────────────────
    Words are accumulated in *logical* (source) order.  Before checking
    whether a candidate line fits, we apply get_display() to get its visual
    width — Hebrew glyph widths are the same in logical vs. visual order, so
    this measurement is valid.

    Once each logical line is complete, get_display(line, base_dir='R') is
    applied a final time to produce the *display* string that Pillow draws.

    base_dir='R' is explicit so lines beginning with digits or Latin chars
    ("5 חיילים", "Chapter 3") are still treated as RTL paragraphs.
    """
    text = text.strip()
    if not text:
        return []

    words          = text.split()
    logical_lines: list[str] = []
    current:       list[str] = []

    for word in words:
        candidate        = " ".join(current + [word])
        visual_candidate = _bidi(candidate)
        w                = _measure_width(visual_candidate, font)

        if w <= max_width or not current:
            # Word fits (or it's the only word on the line — can't shrink further)
            current.append(word)
        else:
            logical_lines.append(" ".join(current))
            current = [word]

    if current:
        logical_lines.append(" ".join(current))

    # Apply BiDi to each completed logical line → visual display order
    return [_bidi(line) for line in logical_lines]


def _bidi(text: str) -> str:
    """
    Apply the Unicode BiDi algorithm, forcing right-to-left base direction.

    The try/except handles the rare case where an older python-bidi version
    does not accept the base_dir keyword argument.
    """
    try:
        return get_display(text, base_dir="R")
    except TypeError:
        return get_display(text)


def _measure_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    """Return the pixel width of text rendered with font."""
    try:
        return int(font.getlength(text))   # Pillow >= 9.2 — preferred
    except AttributeError:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]


# ---------------------------------------------------------------------------
# PDF assembly
# ---------------------------------------------------------------------------

def _assemble_pdf(output_dir: Path) -> None:
    """
    Combine all typeset PNGs in output/ into output/result.pdf.

    Only files whose stem is all-digits are included (our "001.png" naming
    convention), so any stray debug images are automatically excluded.
    Files are sorted lexicographically — "001" < "002" < … — which is
    correct because all stems are zero-padded to the same width.
    """
    page_paths = sorted(
        p for p in output_dir.glob("*.png")
        if p.stem.isdigit()
    )

    if not page_paths:
        raise RuntimeError(
            "No typeset page images found in output/. "
            "Typesetting may have failed for every page."
        )

    result_path = output_dir / "result.pdf"
    with open(result_path, "wb") as fh:
        fh.write(img2pdf.convert([str(p) for p in page_paths]))

    log.info(
        "[typesetter] PDF assembled: %d page(s) → %s",
        len(page_paths),
        result_path,
    )
