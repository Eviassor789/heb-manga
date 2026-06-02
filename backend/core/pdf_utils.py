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
    quality: int = 85,          # 85 % JPEG quality — good balance for manga.
                                 # Typesetter already saves at q=85; using the same
                                 # value here avoids double JPEG degradation.
                                 # Lower to 65 if you need a smaller compressed PDF.
    max_width: int = 1500,      # Downscale pages wider than this (manga is usually 1000–1800 px).
                                 # 1500 px is indistinguishable from the original at normal
                                 # reading sizes while meaningfully reducing file size.
    save_pages: bool = False,
) -> None:
    """
    Build a JPEG-compressed PDF from typeset output pages.

    Accepts both JPEG (.jpg) and PNG (.png) input — typesetter now writes .jpg
    directly so the PNG path is kept only for backward compatibility with old jobs.

    For JPEG inputs that don't need downscaling the raw JPEG bytes are embedded
    directly into the PDF (no re-encode → no quality loss).

    save_pages=True also writes individual JPEGs to output_dir/pages/NNN.jpg.
    These are uploaded to R2 so the web reader can show pages as plain <img>
    elements — no PDF.js or server-side streaming needed.
    """
    page_paths = sorted(
        p for p in output_dir.iterdir()
        if p.stem.isdigit() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    if not page_paths:
        raise RuntimeError("No output pages found to compress.")

    pages_dir = output_dir / "pages"
    if save_pages:
        pages_dir.mkdir(exist_ok=True)

    jpeg_blobs: list[bytes] = []
    for p in page_paths:
        is_jpeg = p.suffix.lower() in (".jpg", ".jpeg")

        if is_jpeg:
            # Read dimensions without full decode to check if downscaling is needed
            img = Image.open(p)
            needs_downscale = img.width > max_width
            img.close()

            if not needs_downscale:
                # Embed the original JPEG bytes — zero quality loss
                blob = p.read_bytes()
            else:
                # Re-open fully, downscale, re-encode
                img = Image.open(p).convert("RGB")
                new_height = int(img.height * max_width / img.width)
                img = img.resize((max_width, new_height), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
                blob = buf.getvalue()
        else:
            # PNG input (legacy / backward compat) — encode to JPEG
            img = Image.open(p).convert("RGB")
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
