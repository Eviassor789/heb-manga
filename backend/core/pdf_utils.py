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
    quality: int = 65,          # 65 % JPEG quality — good balance for manga.
                                 # Line-art stays crisp; smooth-gradient panels are clean.
                                 # Raise to 75 if quality complaints arise, lower to 50 to save more space.
    max_width: int = 1500,      # Downscale pages wider than this (manga is usually 1000–1800 px).
                                 # 1500 px is indistinguishable from the original at normal
                                 # reading sizes while meaningfully reducing file size.
    save_pages: bool = False,
) -> None:
    """
    Re-encode typeset output PNGs as JPEG and assemble a PDF.

    Combined effect of quality=50 + max_width=1500:
      ~7 MB  →  ~2 MB per chapter; still sharp for manga line-art in a browser reader.

    save_pages=True also writes individual JPEGs to output_dir/pages/NNN.jpg.
    These are uploaded to R2 so the web reader can show pages as plain <img>
    elements — no PDF.js or server-side streaming needed.
    """
    page_paths = sorted(p for p in output_dir.glob("*.png") if p.stem.isdigit())
    if not page_paths:
        raise RuntimeError("No output pages found to compress.")

    pages_dir = output_dir / "pages"
    if save_pages:
        pages_dir.mkdir(exist_ok=True)

    jpeg_blobs: list[bytes] = []
    for p in page_paths:
        img = Image.open(p).convert("RGB")

        # Downscale if wider than max_width (maintain aspect ratio)
        if img.width > max_width:
            new_height = int(img.height * max_width / img.width)
            img = img.resize((max_width, new_height), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
        blob = buf.getvalue()
        jpeg_blobs.append(blob)

        if save_pages:
            (pages_dir / f"{p.stem}.jpg").write_bytes(blob)

    with open(dest, "wb") as fh:
        fh.write(img2pdf.convert(jpeg_blobs))
