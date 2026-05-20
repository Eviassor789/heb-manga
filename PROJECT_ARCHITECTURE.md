# Hebrew Manga Translator — Project Architecture

> **Status:** MVP Design Phase  
> **Last Updated:** 2026-05-20  
> **Stack Philosophy:** Local/free for heavy compute; Gemini Free Tier for translation.

---

## 1. Project Overview

A full-stack web application that accepts a manga or comic file (ZIP of images or PDF), runs it through a 5-step translation pipeline, and returns a clean Hebrew-typeset file. Every computationally heavy step (image processing, OCR, inpainting, typesetting) runs locally and free. Only the translation step uses an external API (Google Gemini Free Tier).

### Core User Flow
```
Upload ZIP/PDF → Page Splitting → Text Detection & OCR → Inpainting → Translation → Typesetting → Download
```

---

## 2. Technical Stack

### Frontend
| Concern | Technology |
|---|---|
| Framework | Next.js 14 (App Router) |
| UI Library | Tailwind CSS + shadcn/ui |
| File Upload | react-dropzone |
| Progress Feedback | Server-Sent Events (SSE) from FastAPI |
| State Management | React `useState` / `useReducer` (no Redux needed at MVP) |

### Backend
| Concern | Technology |
|---|---|
| API Server | Python 3.11 + FastAPI |
| Task Queue | In-process async (asyncio); upgrade to Celery+Redis if multi-user concurrency is needed |
| File Handling | `pypdf` (PDF splitting), `Pillow` (image I/O), `zipfile` (ZIP extraction) |
| Text Detection | `comic-text-detector` (YOLO-based, trained on comic speech bubbles) |
| OCR | `EasyOCR` (English, no GPU required) |
| Inpainting | `LaMa` (Large Mask Inpainting — local model, ~512 MB) |
| Translation | Google Gemini API via `google-generativeai` SDK |
| Typesetting | `Pillow` + `python-bidi` + `arabic-reshaper` |
| Font | Heebo / Frank Ruhl Libre (Hebrew comic-compatible, open license) |

### Infrastructure (MVP — Zero Cost)
| Concern | Choice |
|---|---|
| Frontend Hosting | Vercel (free tier) |
| Backend Hosting | Local dev only at MVP; Render.com free tier for staging |
| Model Storage | Local filesystem (models downloaded on first run, cached) |
| Job Artifacts | Local `data/jobs/<uuid>/` directory per job |

---

## 3. Directory Structure

```
HebrewMangaTranslator/
├── PROJECT_ARCHITECTURE.md        # This file
│
├── frontend/                      # Next.js application
│   ├── app/
│   │   ├── page.tsx               # Upload UI — home page
│   │   ├── result/[jobId]/
│   │   │   └── page.tsx           # Download & preview page
│   │   └── layout.tsx
│   ├── components/
│   │   ├── FileUploader.tsx
│   │   ├── ProgressTracker.tsx    # Reads SSE stream from backend
│   │   └── ResultViewer.tsx
│   ├── lib/
│   │   └── api.ts                 # Typed fetch wrappers for FastAPI
│   ├── public/
│   └── package.json
│
├── backend/                       # FastAPI application
│   ├── main.py                    # FastAPI app entrypoint, routes
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── splitter.py            # Step 0: PDF/ZIP → individual page images
│   │   ├── detector.py            # Step 1: Text detection (comic-text-detector)
│   │   ├── ocr.py                 # Step 2: EasyOCR text extraction per bounding box
│   │   ├── inpainter.py           # Step 3: LaMa inpainting (erase text regions)
│   │   ├── translator.py          # Step 4: Gemini API translation with rate limiting
│   │   └── typesetter.py          # Step 5: Hebrew RTL typesetting with Pillow
│   ├── models/                    # Downloaded model weights (gitignored)
│   │   ├── lama/
│   │   └── comic_text_detector/
│   ├── fonts/                     # Hebrew fonts (OFL licensed)
│   │   └── Heebo-Bold.ttf
│   ├── data/
│   │   └── jobs/                  # Per-job working directory
│   │       └── <uuid>/
│   │           ├── original/      # Raw uploaded pages
│   │           ├── detection/     # JSON bounding box data per page
│   │           ├── cleaned/       # Inpainted (text-erased) images
│   │           ├── translated/    # JSON with Hebrew translations per page
│   │           └── output/        # Final typeset images + assembled PDF
│   ├── utils/
│   │   ├── bidi_renderer.py       # Hebrew BiDi + text-fitting utilities
│   │   ├── rate_limiter.py        # Gemini 429 exponential backoff handler
│   │   └── job_manager.py         # Job state tracking, SSE event emitter
│   ├── requirements.txt
│   └── .env                       # GEMINI_API_KEY (gitignored)
│
├── .gitignore
└── README.md
```

---

## 4. Data Pipeline — Step by Step

### Step 0: File Ingestion & Page Splitting
**Module:** `backend/pipeline/splitter.py`

- Accept multipart upload of `.zip` or `.pdf`
- Generate a UUID job ID; create `data/jobs/<uuid>/` directory tree
- **PDF:** Use `pypdf` to extract each page as a PNG at 150 DPI
- **ZIP:** Extract image files (JPG/PNG/WebP), sort by filename
- Output: numbered page images in `data/jobs/<uuid>/original/`
- Emit SSE event: `{ stage: "split", progress: 100 }`

### Step 1: Text Detection
**Module:** `backend/pipeline/detector.py`  
**Model:** `comic-text-detector` (YOLO variant, ~50MB)

- Run each page image through the detector
- Output per page: a JSON file with an array of detected regions:
  ```json
  {
    "page": "001.png",
    "regions": [
      {
        "id": 0,
        "bbox": [x1, y1, x2, y2],
        "mask_polygon": [[x,y], ...],
        "type": "dialogue"
      }
    ]
  }
  ```
- Region types: `dialogue`, `narration`, `sfx` (SFX regions are flagged but not translated at MVP)
- Output: JSON files in `data/jobs/<uuid>/detection/`
- Emit SSE event: `{ stage: "detect", page: N, progress: P }`

### Step 2: OCR (Text Extraction)
**Module:** `backend/pipeline/ocr.py`  
**Model:** `EasyOCR` (English)

- For each `dialogue`/`narration` region from Step 1, crop the bounding box from the original image
- Run EasyOCR on the cropped region
- Append extracted text to the detection JSON:
  ```json
  { "id": 0, "bbox": [...], "type": "dialogue", "source_text": "Let me go!" }
  ```
- Confidence threshold: discard regions where EasyOCR confidence < 0.5
- Output: enriched JSON files in `data/jobs/<uuid>/detection/` (same files, updated in-place)

### Step 3: Inpainting (Text Erasure)
**Module:** `backend/pipeline/inpainter.py`  
**Model:** `LaMa` (Large Mask Inpainting, ~512 MB)

- For each page, build a binary mask image: white pixels where text regions are, black elsewhere
- Run LaMa with `(original_image, mask)` → `cleaned_image`
- LaMa seamlessly fills manga backgrounds, screen tones, and gradients
- Output: cleaned images in `data/jobs/<uuid>/cleaned/`
- **Performance note:** CPU inference ~15-30s/page; GPU ~1-3s/page
- Emit SSE event: `{ stage: "inpaint", page: N, progress: P }`

### Step 4: Context-Aware Translation
**Module:** `backend/pipeline/translator.py`  
**API:** Google Gemini 2.0 Flash (Free Tier — 1,500 requests/day, 15 RPM)

#### Per-page Gemini Request
Batch all `source_text` values from a single page into one structured JSON prompt:

```python
SYSTEM_PROMPT = """
You are a professional manga translator. Translate the following English comic text to natural, 
colloquial Hebrew. Preserve character voice, gender, and emotional tone. Do NOT transliterate — 
use natural Hebrew expressions. Sound effects (marked type:sfx) should be translated creatively.

Character glossary (maintain consistency):
{glossary_json}

Return ONLY a valid JSON array with the same structure, adding a "hebrew_text" field to each object.
"""

payload = [
    {"id": 0, "source_text": "Let me go!", "type": "dialogue"},
    {"id": 1, "source_text": "BANG", "type": "sfx"}
]
```

#### Glossary System
- A `glossary.json` file is maintained per job: `{ "Alex": "אלכס", "Zoe": "זואי" }`
- After each page translation, the translator parses character names and updates the glossary
- The glossary is prepended to every subsequent page's Gemini system prompt

#### Rate Limiting Handler (`utils/rate_limiter.py`)
```python
# Exponential backoff on HTTP 429
MAX_RETRIES = 5
BASE_DELAY  = 60   # seconds (Gemini free tier resets per minute)

async def call_gemini_with_backoff(prompt):
    for attempt in range(MAX_RETRIES):
        try:
            return await gemini_client.generate(prompt)
        except RateLimitError:
            delay = BASE_DELAY * (2 ** attempt)
            await asyncio.sleep(delay)
    raise TranslationError("Gemini rate limit exceeded after retries")
```

- Output: JSON files in `data/jobs/<uuid>/translated/`

### Step 5: Hebrew Typesetting
**Module:** `backend/pipeline/typesetter.py`  
**Libraries:** `Pillow`, `python-bidi`, `arabic-reshaper`

#### BiDi Rendering (`utils/bidi_renderer.py`)
Raw Hebrew strings must be processed through the Unicode BiDi algorithm before Pillow renders them:

```python
from bidi.algorithm import get_display
from arabic_reshaper import reshape   # only for mixed strings

def prepare_hebrew(text: str) -> str:
    return get_display(text)  # applies RTL reordering
```

#### Text-Fitting Algorithm
For each translated region on a page:
1. Open the cleaned image from Step 3
2. Calculate the bounding box dimensions `(width, height)`
3. **Auto-shrink loop:**
   - Start at `font_size = 24`
   - Wrap text to fit within `width - padding`
   - Measure total text block height
   - If it overflows `height`, reduce `font_size -= 1` and retry
   - Minimum font size: `8` (flag as warning if reached)
4. Draw text centered horizontally, aligned to top of bbox
5. Text alignment: `align="center"`, RTL direction via BiDi preprocessing

#### Font Stack
- Primary: `Heebo-Bold.ttf` (clean, legible at small sizes, OFL license)
- Fallback: `Frank Ruhl Libre` (more traditional, serif)
- Both available on Google Fonts

#### Output Assembly
- Composite typeset text onto cleaned images
- After all pages are done, assemble into a PDF using `img2pdf` or `Pillow`
- Output: `data/jobs/<uuid>/output/result.pdf` + individual page PNGs

---

## 5. API Contract (FastAPI ↔ Next.js)

### `POST /api/jobs`
- **Body:** `multipart/form-data` with `file` field
- **Response:** `{ "job_id": "<uuid>" }`
- Starts the pipeline as a background asyncio task

### `GET /api/jobs/{job_id}/status`
- **Response:** SSE stream of `text/event-stream`
- Events:
  ```
  data: {"stage": "split",    "progress": 100}
  data: {"stage": "detect",   "page": 1, "total": 12, "progress": 8}
  data: {"stage": "inpaint",  "page": 1, "total": 12, "progress": 8}
  data: {"stage": "translate","page": 1, "total": 12, "progress": 8}
  data: {"stage": "typeset",  "page": 1, "total": 12, "progress": 8}
  data: {"stage": "done",     "download_url": "/api/jobs/<uuid>/download"}
  data: {"stage": "error",    "message": "..."}
  ```

### `GET /api/jobs/{job_id}/download`
- Streams the output `result.pdf` as `application/pdf`

### `DELETE /api/jobs/{job_id}`
- Cleans up the job directory from disk

---

## 6. Hebrew RTL — Critical Notes

These are the most common failure points for Hebrew typesetting:

| Issue | Root Cause | Fix |
|---|---|---|
| Reversed character order | Pillow renders LTR | Run all strings through `python-bidi` `get_display()` before drawing |
| Letters appear as isolated glyphs | Not a Hebrew issue (that's Arabic shaping) | No reshaper needed for Hebrew, but verify font has full Unicode Hebrew block |
| Text overflows bubble | Hebrew ≠ same pixel width as English | Use auto-shrink font loop (see Step 5) |
| Mixed Hebrew+English numbers | BiDi algorithm edge case | Always test strings like `"5 חיילים"` and `"Chapter 5"` |
| Line breaks wrong | Pillow wraps LTR | Manually split lines before passing to `get_display()`, wrap each line separately |

---

## 7. Environment Variables

```bash
# backend/.env
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.0-flash
MAX_PAGES_PER_JOB=50
LAMA_MODEL_PATH=./models/lama/big-lama.pth
DETECTOR_MODEL_PATH=./models/comic_text_detector/comictextdetector.pt
```

---

## 8. Python Dependencies (`requirements.txt`)

```
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
python-multipart>=0.0.9
pypdf>=4.2.0
Pillow>=10.3.0
easyocr>=1.7.1
python-bidi>=0.4.2
arabic-reshaper>=3.0.0
google-generativeai>=0.7.0
img2pdf>=0.5.1
torch>=2.3.0          # Required by EasyOCR and LaMa
torchvision>=0.18.0
opencv-python>=4.9.0
omegaconf>=2.3.0      # Required by LaMa
# comic-text-detector: install via git (see setup instructions)
```

---

## 9. Out-of-Scope for MVP (Future V2 Features)

| Feature | Reason Deferred |
|---|---|
| SFX translation & re-rendering | Stylized art-embedded text; requires generative inpainting + font matching |
| Multi-language output (not just Hebrew) | Trivial to add later; just swap Gemini prompt language |
| Celery/Redis task queue | Needed only when multi-user concurrency becomes a requirement |
| Cloud GPU inference | Cost; LaMa on CPU is acceptable for 50-page MVP jobs |
| User accounts & job history | No auth system at MVP |
| Double-page spread handling | Complex layout; treat as two separate pages for now |

---

## 10. Setup & First Run (Quickstart)

```bash
# 1. Clone & set up backend
cd backend
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt

# 2. Download models (first run only)
python utils/download_models.py

# 3. Set environment variables
cp .env.example .env
# Edit .env → add your GEMINI_API_KEY

# 4. Start backend
uvicorn main:app --reload --port 8000

# 5. Set up frontend (separate terminal)
cd frontend
npm install
npm run dev    # Starts at http://localhost:3000
```

---

*This document is the single source of truth for the project architecture. Update it whenever a significant technical decision is made.*
