"""
Step 0 — File Splitter

Accepts a .pdf or .zip source file and produces numbered PNG pages in:
    <job_dir>/original/001.png, 002.png, ...

PDF rendering uses PyMuPDF (fitz), which is self-contained (no poppler needed).
ZIP extraction normalises filenames with natural sort so page order is preserved
even when the archive uses names like "page1.jpg", "page10.jpg", "page2.jpg".
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from core.job_manager import EmitFn

# Image formats we'll accept from a ZIP archive
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

# PDF render resolution — 150 DPI is a good balance of quality vs. file size
_PDF_DPI = 150
_PDF_SCALE = _PDF_DPI / 72  # fitz default is 72 DPI


async def split(job_dir: Path, source_file: Path, emit: EmitFn) -> list[Path]:
    """
    Split source_file into individual PNG pages.

    Returns a sorted list of absolute paths to the produced pages
    (all inside <job_dir>/original/).
    """
    await emit({"stage": "split", "status": "running"})
    original_dir = job_dir / "original"

    suffix = source_file.suffix.lower()
    if suffix == ".pdf":
        pages = await _split_pdf(source_file, original_dir, emit)
    elif suffix == ".zip":
        pages = await _split_zip(source_file, original_dir, emit)
    else:
        raise ValueError(f"Unsupported source format: {suffix!r}")

    if not pages:
        raise ValueError("No pages could be extracted from the uploaded file.")

    await emit({"stage": "split", "status": "done", "total_pages": len(pages)})
    return pages


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

async def _split_pdf(pdf_path: Path, out_dir: Path, emit: EmitFn) -> list[Path]:
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    matrix = fitz.Matrix(_PDF_SCALE, _PDF_SCALE)
    pages: list[Path] = []

    for i, page in enumerate(doc, start=1):
        out_path = out_dir / f"{i:03d}.png"
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(out_path))
        pages.append(out_path)
        await emit({"stage": "split", "status": "running", "page": i, "total": total})

    doc.close()
    return pages


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------

async def _split_zip(zip_path: Path, out_dir: Path, emit: EmitFn) -> list[Path]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Collect all image entries, ignoring macOS metadata folders
        image_entries = [
            name for name in zf.namelist()
            if not _is_hidden(name)
            and Path(name).suffix.lower() in _IMAGE_SUFFIXES
        ]

        if not image_entries:
            raise ValueError("ZIP archive contains no supported image files (jpg/png/webp/bmp/tiff).")

        image_entries.sort(key=_natural_sort_key)
        total = len(image_entries)
        pages: list[Path] = []

        for i, entry in enumerate(image_entries, start=1):
            raw_data = zf.read(entry)
            out_path = out_dir / f"{i:03d}.png"
            _save_as_png(raw_data, out_path)
            pages.append(out_path)
            await emit({"stage": "split", "status": "running", "page": i, "total": total})

    return pages


def _save_as_png(raw_bytes: bytes, dest: Path) -> None:
    """Convert any supported image format to PNG and save."""
    import io
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    img.save(dest, format="PNG")


def _is_hidden(name: str) -> bool:
    """Filter out __MACOSX metadata and dotfile entries from ZIPs."""
    parts = Path(name).parts
    return any(p.startswith((".", "__")) for p in parts)


def _natural_sort_key(name: str) -> list[int | str]:
    """
    Split a filename into alternating text/number chunks so that
    'page10.jpg' sorts after 'page9.jpg' rather than after 'page1.jpg'.
    """
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", Path(name).name)
    ]
