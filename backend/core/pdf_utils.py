"""
PDF utilities — building compressed PDFs and saving individual JPEG pages.

Used by:
  main.py      → _build_compressed_pdf (download endpoint, backward-compat wrapper)
  library.py   → build_compressed_pdf (with save_pages=True for R2 upload)
"""

from __future__ import annotations

import io
from pathlib import Path

import img2pdf
from PIL import Image


def build_compressed_pdf(
    output_dir: Path,
    dest: Path,
    quality: int = 85,
    save_pages: bool = False,
) -> None:
    """
    Re-encode typeset output PNGs as JPEG and assemble a PDF.

    quality=85 is the sweet spot: visually identical to lossless at ~15-20 % of
    the PNG size.  Manga line art survives 85 % JPEG quality well.

    save_pages=True also writes individual JPEGs to output_dir/pages/NNN.jpg.
    These are later uploaded to R2 so the web reader can display pages as
    plain <img> elements — no PDF.js or server-side streaming needed.
    """
    page_paths = sorted(p for p in output_dir.glob("*.png") if p.stem.isdigit())
    if not page_paths:
        raise RuntimeError("No output pages found to compress.")

    pages_dir = output_dir / "pages"
    if save_pages:
        pages_dir.mkdir(exist_ok=True)

    jpeg_blobs: list[bytes] = []
    for p in page_paths:
        img  = Image.open(p).convert("RGB")
        buf  = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
        blob = buf.getvalue()
        jpeg_blobs.append(blob)

        if save_pages:
            (pages_dir / f"{p.stem}.jpg").write_bytes(blob)

    with open(dest, "wb") as fh:
        fh.write(img2pdf.convert(jpeg_blobs))
