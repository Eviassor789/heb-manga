#!/usr/bin/env python3
"""
First-run model weight setup.

Run from the backend/ directory:
    python utils/download_models.py

What this downloads
───────────────────
  [1] comic-text-detector weights  comictextdetector.pt  (~35 MB)
      Source: github.com/zyddnys/manga-image-translator — beta-0.2.1 release
      Note:  the model lives in the manga-image-translator repo, NOT the
             comic-text-detector repo (which has no releases).

  [2] LaMa inpainting model        (~200 MB, auto-managed)
      Source: Hugging Face Hub via simple-lama-inpainting
      The model downloads automatically to your HF cache on first use.
      This step just triggers that download so it doesn't surprise you at
      runtime. Requires: pip install simple-lama-inpainting

Prerequisites (must be done first — one-time)
─────────────────────────────────────────────
  cd backend
  git clone https://github.com/dmMaze/comic-text-detector vendor/comic-text-detector
  pip install -r requirements.txt
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parent.parent
_MODELS = _BACKEND / "models"

_DETECTOR_DIR = _MODELS / "comic_text_detector"
_DETECTOR_PT = _DETECTOR_DIR / "comictextdetector.pt"

# The model lives in manga-image-translator's releases, not comic-text-detector's.
# Direct .pt file — no zip wrapper.
_DETECTOR_URL = (
    "https://github.com/zyddnys/manga-image-translator"
    "/releases/download/beta-0.2.1/comictextdetector.pt"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _progress_hook(block_count: int, block_size: int, total_size: int) -> None:
    downloaded = min(block_count * block_size, total_size)
    if total_size > 0:
        pct = downloaded / total_size * 100
        bar = "#" * int(pct // 2)
        sys.stdout.write(f"\r  [{bar:<50}] {pct:5.1f}%")
        sys.stdout.flush()
        if downloaded >= total_size:
            print()


def _download_url(url: str, dest: Path) -> None:
    print(f"  → {url}")
    urllib.request.urlretrieve(url, str(dest), reporthook=_progress_hook)


def _check_vendor() -> None:
    vendor = _BACKEND / "vendor" / "comic-text-detector"
    if not vendor.exists():
        print("\n[WARNING] comic-text-detector source not found.")
        print(f"  Expected: {vendor}")
        print("  Run:")
        print("    cd backend")
        print("    git clone https://github.com/dmMaze/comic-text-detector vendor/comic-text-detector")
        print()


# ---------------------------------------------------------------------------
# Step 1: comic-text-detector weights
# ---------------------------------------------------------------------------

def download_detector() -> None:
    print("\n[1/2] comic-text-detector weights")

    if _DETECTOR_PT.exists():
        print(f"  Already present: {_DETECTOR_PT}")
        return

    _DETECTOR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _DETECTOR_DIR / "_comictextdetector.tmp"

    try:
        _download_url(_DETECTOR_URL, tmp)
        tmp.rename(_DETECTOR_PT)
        print(f"  Saved → {_DETECTOR_PT}")
        print("  [1/2] Done.")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"\n  Download failed: {exc}")
        print("\n  Manual fallback — open this URL in your browser and save")
        print(f"  the file as 'comictextdetector.pt' into:")
        print(f"    {_DETECTOR_DIR}")
        print(f"\n  URL: {_DETECTOR_URL}")


# ---------------------------------------------------------------------------
# Step 2: LaMa weights  (via simple-lama-inpainting)
# ---------------------------------------------------------------------------

def download_lama() -> None:
    print("\n[2/2] LaMa inpainting model (simple-lama-inpainting)")

    try:
        from simple_lama_inpainting import SimpleLama  # noqa: PLC0415
    except ImportError:
        print("  simple-lama-inpainting is not installed.")
        print("  Run: pip install simple-lama-inpainting")
        print("  Then re-run this script.")
        return

    print("  Instantiating SimpleLama() — this triggers the automatic HuggingFace")
    print("  model download (~200 MB) if not already cached …")
    try:
        SimpleLama()  # downloads model to ~/.cache/huggingface on first call
        print("  LaMa model is ready.")
        print("  [2/2] Done.")
    except Exception as exc:
        print(f"  Model load failed: {exc}")
        print("  Check your internet connection and try again.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print(" Hebrew Manga Translator — First-Run Model Setup")
    print("=" * 55)

    _check_vendor()
    download_detector()
    download_lama()

    print("\nAll done. You can now start the backend:")
    print("  uvicorn main:app --reload --port 8000")
