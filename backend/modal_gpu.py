"""
Modal GPU app — Hebrew Manga Translator
────────────────────────────────────────
Offloads the three CPU-bottleneck pipeline steps to Modal's cloud GPU:
  detect_page   — comic-text-detector  (YOLO + transformer, ~10 min → ~20 s)
  ocr_page      — EasyOCR              (~10 min → ~20 s)
  inpaint_page  — LaMa neural inpaint  (~10 min → ~30 s)

One-time setup (do this once, free):
  pip install modal
  modal token new          ← opens browser, creates free account

Deploy the functions (run from backend/ directory):
  modal deploy modal_gpu.py

Then in backend/.env set:
  USE_MODAL=true

Free tier: 30 GPU-hours/month.  A 23-page chapter uses ~1-2 minutes of GPU time.
That's 900-1 800 free chapters per month.
"""

from __future__ import annotations

from pathlib import Path

import modal

# ── App + shared volume ────────────────────────────────────────────────────────

app = modal.App("hebrew-manga-translator")

# Persistent volume that caches large model downloads across cold starts:
#   EasyOCR models  ~100 MB  (downloaded once, reused forever)
#   LaMa weights    ~200 MB  (via HuggingFace, downloaded once)
model_cache = modal.Volume.from_name("manga-model-cache", create_if_missing=True)

_HERE     = Path(__file__).parent                           # backend/
_VENDOR   = str(_HERE / "vendor" / "comic-text-detector")  # local source
_WEIGHTS  = str(_HERE / "models" / "comic_text_detector")  # local weights dir
_REMOTE_VENDOR  = "/app/vendor/comic-text-detector"
_REMOTE_WEIGHTS = "/app/models/comic_text_detector/comictextdetector.pt"
_CACHE    = "/model-cache"

# ── Container image ────────────────────────────────────────────────────────────
# Built once by Modal and cached.  Rebuilt only when pip_install list changes.
# Local files (vendor code, model weights) are provided via Mounts — they are
# attached at call time, not baked into the image.  This means code changes in
# vendor/ are picked up immediately without a slow image rebuild.

_image = (
    modal.Image.debian_slim(python_version="3.11")
    # System libs required by OpenCV headless
    .apt_install(["libgl1", "libglib2.0-0"])
    .pip_install([
        # PyTorch — CUDA build (T4 is CUDA 12.x compatible)
        "torch==2.3.1",
        "torchvision==0.18.1",
        # Computer vision
        "opencv-python-headless>=4.9",
        "numpy>=1.24,<2.0",
        "Pillow>=9.5.0,<10.0.0",   # simple-lama-inpainting requires <10
        # OCR
        "easyocr>=1.7",
        # Inpainting
        "simple-lama-inpainting>=0.1.2",
        # comic-text-detector runtime deps
        "einops>=0.7",
        "timm>=0.9",
        "albumentations>=1.3",
        "scikit-image>=0.21",
        "shapely>=2.0",
        # Common transitive deps missing from debian_slim
        "requests>=2.31",
        "tqdm>=4.66",
        "filelock>=3.13",
        "huggingface_hub>=0.23",
    ])
    # add_local_dir bakes local directories into the image at build time.
    # The image is cached by Modal — rebuilds only when pip_install changes.
    .add_local_dir(_VENDOR,  remote_path=_REMOTE_VENDOR)
    .add_local_dir(_WEIGHTS, remote_path="/app/models/comic_text_detector")
)

# ── Shared decorator kwargs ────────────────────────────────────────────────────

_GPU_KWARGS = dict(
    gpu="T4",
    image=_image,
    volumes={_CACHE: model_cache},
    timeout=300,          # 5-minute max per page (generous)
)

# ── Module-level singletons — survive across warm invocations ─────────────────
# Modal reuses the same container for multiple calls while it's warm, so the
# expensive model-load only happens once per cold start.

_detector_inst = None
_ocr_reader    = None
_lama_inst     = None


# ──────────────────────────────────────────────────────────────────────────────
# 1. DETECT
# ──────────────────────────────────────────────────────────────────────────────

@app.function(**_GPU_KWARGS, memory=4096)
def detect_page(img_bytes: bytes) -> dict:
    """
    Detect speech bubbles and text regions in one manga page.

    Input  : PNG image as raw bytes
    Output : {
        "image_size": {"width": int, "height": int},
        "regions":    [ {id, bbox, polygon, type, vertical, confidence,
                         source_text: null, hebrew_text: null}, ... ],
        "mask_b64":   "<base64-encoded PNG mask>"
    }
    """
    global _detector_inst
    import base64, json, sys
    import cv2
    import numpy as np

    if _REMOTE_VENDOR not in sys.path:
        sys.path.insert(0, _REMOTE_VENDOR)

    # ── Load model (once per cold start) ─────────────────────────────────────
    if _detector_inst is None:
        from inference import TextDetector  # noqa: PLC0415
        _detector_inst = TextDetector(
            model_path=_REMOTE_WEIGHTS,
            input_size=1024,
            device="cuda",
            act="leaky",
        )

    # ── Decode image ──────────────────────────────────────────────────────────
    arr     = np.frombuffer(img_bytes, np.uint8)
    img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    h, w    = img_bgr.shape[:2]

    # ── Run detector ──────────────────────────────────────────────────────────
    _mask_raw, mask_refined, blk_list = _detector_inst(img_bgr)

    kernel       = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask_dilated = cv2.dilate(mask_refined, kernel, iterations=2)

    # Encode mask → PNG bytes → base64 string (JSON-serialisable)
    _, mask_buf = cv2.imencode(".png", mask_dilated)
    mask_b64    = base64.b64encode(mask_buf.tobytes()).decode()

    # ── Build regions list ────────────────────────────────────────────────────
    regions: list[dict] = []
    for idx, blk in enumerate(blk_list):
        try:
            x1, y1, x2, y2 = (int(v) for v in blk.xyxy[:4])
            if x2 <= x1 or y2 <= y1:
                continue
        except Exception:
            continue

        regions.append({
            "id":          idx,
            "bbox":        [x1, y1, x2, y2],
            "polygon":     [[x1,y1],[x2,y1],[x2,y2],[x1,y2]],
            "type":        "sfx" if getattr(blk, "vertical", False) else "dialogue",
            "vertical":    bool(getattr(blk, "vertical", False)),
            "confidence":  round(float(getattr(blk, "prob", 1.0)), 4),
            "source_text": None,
            "hebrew_text": None,
        })

    return {"image_size": {"width": w, "height": h}, "regions": regions, "mask_b64": mask_b64}


# ──────────────────────────────────────────────────────────────────────────────
# 2. OCR
# ──────────────────────────────────────────────────────────────────────────────

@app.function(**_GPU_KWARGS, memory=4096)
def ocr_page(img_bytes: bytes, regions: list[dict]) -> list[dict]:
    """
    OCR every dialogue region in the page.

    Input  : PNG image bytes + regions list (from detect_page output)
    Output : same regions list with source_text populated
    """
    global _ocr_reader
    import re
    import cv2
    import numpy as np

    # ── Load EasyOCR reader (once per cold start) ─────────────────────────────
    if _ocr_reader is None:
        import easyocr  # noqa: PLC0415
        _ocr_reader = easyocr.Reader(
            lang_list=["en"],
            gpu=True,
            model_storage_directory=f"{_CACHE}/easyocr",
            download_enabled=True,
            verbose=False,
        )

    # ── Decode image ──────────────────────────────────────────────────────────
    arr     = np.frombuffer(img_bytes, np.uint8)
    img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img_h, img_w = img_rgb.shape[:2]

    for region in regions:
        if region.get("type") == "sfx":
            continue

        x1, y1, x2, y2 = region["bbox"]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img_w, x2), min(img_h, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        crop = img_rgb[y1:y2, x1:x2]

        # Pad + upscale (same logic as local ocr.py)
        crop  = cv2.copyMakeBorder(crop, 10, 10, 10, 10,
                                   cv2.BORDER_CONSTANT, value=(255, 255, 255))
        h_c, w_c = crop.shape[:2]
        if min(h_c, w_c) < 48 and min(h_c, w_c) > 0:
            s    = 48 / min(h_c, w_c)
            crop = cv2.resize(crop, (max(1, round(w_c*s)), max(1, round(h_c*s))),
                              interpolation=cv2.INTER_CUBIC)

        try:
            results = _ocr_reader.readtext(crop, detail=1, paragraph=False)
        except Exception:
            region["source_text"] = None
            continue

        filtered = [(b, t.strip(), c) for b, t, c in results
                    if c >= 0.30 and t.strip()]
        if not filtered:
            region["source_text"] = None
            continue

        filtered.sort(key=lambda r: (r[0][0][1], r[0][0][0]))
        raw = " ".join(t for _, t, _ in filtered)
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) == 1 and not raw.isalpha():
            raw = ""

        region["source_text"] = raw or None

    return regions


# ──────────────────────────────────────────────────────────────────────────────
# 3. INPAINT
# ──────────────────────────────────────────────────────────────────────────────

@app.function(**_GPU_KWARGS, memory=8192)
def inpaint_page(img_bytes: bytes, mask_b64: str) -> bytes:
    """
    Erase text from a page using LaMa neural inpainting.

    Input  : PNG image bytes + base64-encoded mask PNG (from detect_page output)
    Output : cleaned page as PNG bytes
    """
    global _lama_inst
    import base64, io, os
    import numpy as np
    import cv2
    from PIL import Image

    # Redirect HuggingFace downloads to the persistent volume
    os.environ["HF_HOME"] = f"{_CACHE}/huggingface"

    # ── Load LaMa (once per cold start) ──────────────────────────────────────
    if _lama_inst is None:
        from simple_lama_inpainting import SimpleLama  # noqa: PLC0415
        _lama_inst = SimpleLama()

    # ── Decode image ──────────────────────────────────────────────────────────
    arr     = np.frombuffer(img_bytes, np.uint8)
    img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    image_pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))

    # ── Decode mask ───────────────────────────────────────────────────────────
    mask_pil = Image.open(io.BytesIO(base64.b64decode(mask_b64))).convert("L")
    mask_np  = np.array(mask_pil, dtype=np.uint8)

    # Fast-path: nothing to inpaint
    if mask_np.max() == 0:
        buf = io.BytesIO()
        image_pil.save(buf, format="PNG")
        return buf.getvalue()

    # ── LaMa inference ────────────────────────────────────────────────────────
    result = _lama_inst(image_pil, mask_pil).convert("RGB")
    if result.size != image_pil.size:
        result = result.resize(image_pil.size, Image.LANCZOS)

    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()
