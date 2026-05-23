# Hebrew Manga Translator — Codebase Map

Full-stack app: FastAPI backend (Python) + Next.js 14 App Router frontend (TypeScript).
Translates manga chapters to Hebrew via a 6-stage AI pipeline.

---

## Root Layout

```
HebrewMangaTranslator/
├── backend/          FastAPI server + AI pipeline
├── frontend/         Next.js 14 app (App Router)
└── CLAUDE.md         ← this file
```

---

## Backend

### Entry Points

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app — **all HTTP routes** live here. Startup scan, job orchestration, WeebCentral scraping, library API, SSE streaming, local file serving. ~1 200 lines. |
| `backend/modal_gpu.py` | Modal.com GPU app — offloads detect/OCR/inpaint to cloud GPU. Deploy with `modal deploy modal_gpu.py`. Set `USE_MODAL=true` in `.env` to activate. |
| `backend/.env` | Secrets: `GEMINI_API_KEY`, `R2_*`, `SUPABASE_*`, `USE_MODAL`, `HEBREW_FONT_PATH` |

### `backend/core/` — Infrastructure & Services

| File | Purpose |
|------|---------|
| `job_manager.py` | In-process SSE event bus. `JobManager` holds per-job event history + live subscriber queues. `emit(job_id, event)` broadcasts to all connected clients. `get_emitter()` returns a bound async callable passed into every pipeline step. |
| `library.py` | Chapter persistence — **three modes**: LOCAL (SQLite + disk), HYBRID (SQLite + R2), FULL CLOUD (Supabase + R2). Key functions: `check_cache`, `register_chapter`, `get_all_chapters`, `get_chapter`. Schema has: `id`, `mangadex_id` (or `wc:ULID`), `manga_id`, `manga_title`, `chapter_num`, `cover_url`, `pdf_url`, `pages_prefix`. |
| `manga_downloader.py` | Multi-source downloader: **MangaDex** (public API) + **WeebCentral** (API + HTML scraping). Downloads pages as `original/001.png … NNN.png`. Writes `chapter_meta.json` with title, chapter number, cover URL, `mangadex_id`. |
| `pdf_utils.py` | `build_compressed_pdf()` — assembles PNG pages → JPEG-compressed PDF via img2pdf. Called by library.py for R2 uploads. |
| `rate_limiter.py` | Async exponential backoff for Gemini free-tier (15 RPM). Default: 60 s base, 120 s max, 5 s jitter. |
| `download_models.py` | One-time script to download comic-text-detector weights into `models/`. |

### `backend/pipeline/` — AI Pipeline (6 stages)

Each module exports one async function that takes `(job_dir: Path, emit: EmitFn)` and writes its output to a subdirectory of the job folder.

| File | Stage | Input → Output | Notes |
|------|-------|---------------|-------|
| `splitter.py` | **Step 0** | `.pdf` / `.zip` → `original/NNN.png` | PyMuPDF for PDF, natural-sort ZIP extraction. 150 DPI. |
| `detector.py` | **Step 1** | `original/NNN.png` → `detection/NNN.json` + `detection/NNN_mask.png` | YOLO-based comic-text-detector. Lazy singleton model load. ThreadPoolExecutor. Optional Modal GPU offload. |
| `ocr.py` | **Step 2** | `original/NNN.png` + `detection/NNN.json` → fills `source_text` in JSON | Gemini Vision (primary) — sends all crops in one multipart call per page. EasyOCR fallback. |
| `inpainter.py` | **Step 3** | `original/NNN.png` + `detection/NNN_mask.png` → `cleaned/NNN.png` | LaMa neural inpainting (primary) via simple-lama-inpainting. OpenCV TELEA fallback. Optional Modal GPU offload. |
| `translator.py` | **Step 4** | `detection/NNN.json` (with source_text) → fills `hebrew_text` | Gemini API. Batches all regions per page in one request. Maintains `glossary.json` across pages for consistent proper-noun translation. |
| `typesetter.py` | **Step 5** | `cleaned/NNN.png` + `detection/NNN.json` (with hebrew_text) → `output/NNN.png` + `output/result.pdf` | python-bidi for RTL. Auto-shrink font to fit balloon. Assembles final PDF via img2pdf. |

### `backend/data/` — Runtime Data (git-ignored)

```
data/
├── library.db          SQLite — chapter registry (LOCAL/HYBRID modes)
└── jobs/{uuid}/
    ├── source.*        uploaded file or downloaded pages
    ├── chapter_meta.json
    ├── original/       001.png … NNN.png (raw pages)
    ├── detection/      NNN.json + NNN_mask.png
    ├── cleaned/        NNN.png (text erased)
    ├── output/         NNN.png (typeset) + result.pdf + result_compressed.pdf
    └── glossary.json   grows during translation for consistency
```

### `backend/models/` — AI Model Weights (git-ignored)

```
models/
├── comic_text_detector/   comictextdetector.pt (YOLO)
├── easyocr/               EasyOCR language packs
└── lama/                  LaMa inpainting weights (auto-download via HuggingFace)
```

### `backend/vendor/` — Vendored Source

```
vendor/comic-text-detector/   git submodule — YOLO-based text region detector
    inference.py              main entry point used by pipeline/detector.py
    utils/textblock.py        TextBlock dataclass (bbox, type, mask)
```

### `backend/fonts/` — Hebrew Fonts

Priority: `HEBREW_FONT_PATH` env var → any `.ttf/.otf` in `fonts/` → auto-download Heebo-Bold.ttf.

---

## Backend API Routes (all in `main.py`)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/jobs` | Upload PDF/ZIP → start pipeline |
| `POST` | `/api/jobs/from-url` | MangaDex or WeebCentral URL → download + pipeline. Returns `{job_id}` or `{cached: true, library_id}`. |
| `GET` | `/api/jobs/{id}/status` | SSE stream — emits stage progress events |
| `POST` | `/api/jobs/{id}/resume` | Restart pipeline from a specific step |
| `DELETE` | `/api/jobs/{id}` | Delete job directory |
| `GET` | `/api/jobs/{id}/download` | Download result PDF |
| `GET` | `/api/library` | All translated chapters (flat list) |
| `GET` | `/api/library/{chapter_id}` | Single chapter metadata |
| `GET` | `/api/library/manga/{manga_id}` | All chapters for a manga (by manga_id or WC ULID) |
| `GET` | `/api/library/local-pages/{job_id}/{filename}` | Serve individual page PNGs (LOCAL mode) |
| `POST` | `/api/library/rescan` | Re-scan jobs dir and R2 to rebuild library DB |
| `GET` | `/api/weebcentral/featured` | Scrape WeebCentral "hot updates" |
| `GET` | `/api/weebcentral/series/{ulid}` | Fetch WC series metadata (title, cover, description) |
| `GET` | `/api/weebcentral/series/{ulid}/chapters` | Fetch WC chapter list |
| `GET` | `/api/search/weebcentral?q=` | Search WeebCentral |

---

## Frontend

### Key Identifiers

- **MangaDex IDs**: UUIDs — `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
- **WeebCentral IDs**: ULIDs — 26 uppercase alphanumeric chars, no dashes → pattern `/^[0-9A-HJKMNP-TV-Z]{26}$/i`
- **Library IDs**: UUIDs (Supabase) or `job_{uuid}` (SQLite LOCAL mode)
- **`mangadex_id` in library**: MangaDex chapter UUID, or `wc:{ULID}` for WeebCentral chapters

### `frontend/app/` — Pages (Next.js App Router)

| Route | File | Purpose |
|-------|------|---------|
| `/` | `page.tsx` | **Library homepage** — grid of translated manga series. Groups chapters by manga. `seriesHref()` routes to `/manga/` or `/weebcentral/` based on ID type. |
| `/discover` | `discover/page.tsx` | **Discover page** — search MangaDex API directly from browser + search/browse WeebCentral via backend. Tab toggle: MangaDex ↔ WeebCentral. URL is source of truth (`?q=&src=`). |
| `/manga/[id]` | `manga/[id]/page.tsx` | **MangaDex series page** — fetches series info + chapter list from MangaDex API + library hits. Filter (All/Hebrew/Untranslated), sort, batch navigation (100 chapters/batch). |
| `/weebcentral/[id]` | `weebcentral/[id]/page.tsx` | **WeebCentral series page** — same UX as MangaDex page but backed by `/api/weebcentral/series/{id}`. Filter by translation status. |
| `/translate` | `translate/page.tsx` | **Upload page** — MangaDex URL tab (with live chapter preview) + file upload tab (PDF/ZIP drag-drop). Hits `/api/jobs/from-url` or `/api/jobs`. |
| `/jobs/[id]` | `jobs/[id]/page.tsx` | **Job progress page** — SSE-driven live progress. 6 stage rows with per-stage timers, progress bars, cost breakdown (Gemini tokens + Modal GPU seconds). Resume-from-step on error. Redirects to `/library/{id}` on completion. |
| `/library/[id]` | `library/[id]/page.tsx` | **Reader page** — full-screen manga reader. Two modes: TTB (scroll) + LTR (slide). Zoom, progress bar, keyboard shortcuts. IntersectionObserver tracks current page. Hides NavBar. |

### `frontend/app/api/` — Next.js API Routes

| Route | File | Purpose |
|-------|------|---------|
| `/api/*` | All proxied | Most API calls go directly to `http://localhost:8000` (or `NEXT_PUBLIC_BACKEND_URL`). |
| `/api/jobs/[id]/status` | `route.ts` | **SSE proxy stub** — intentionally empty. Browser connects direct to FastAPI for SSE (Node fetch buffers SSE, making proxying unusable). |

### `frontend/components/` — Shared Components

| File | Purpose |
|------|---------|
| `NavBar.tsx` | Sticky top nav — Library / Discover / Upload links. **Hidden on `/library/*`** (reader is full-screen). Uses `var(--accent)` for active state. |
| `MangaCard.tsx` | Reusable card — cover image + title + subtitle + optional badge. Badge colors: `badge-green` / `badge-violet` / `badge-orange`. CSS class `manga-card-title` enables hover color via pure CSS. |
| `MangaCover.tsx` | Cover image with fallback placeholder. |
| `SkeletonCard.tsx` | Pulsing loading placeholder matching MangaCard dimensions. |
| `Spinner.tsx` | SVG spinner, `size` prop: `"sm"` | `"lg"`. |

### `frontend/app/globals.css` — Design System

CSS custom properties (design tokens):

```css
--accent:            #e879a8   /* sakura pink — ALL interactive elements */
--accent-dim:        #d4628f   /* hover/pressed state */
--accent-glow:       rgba(232,121,168,0.25)
--accent-subtle:     rgba(232,121,168,0.08)
--card-bg:           rgba(255,255,255,0.03)
--card-border:       rgba(139,92,246,0.12)   /* subtle VIOLET trim — bg-level only */
--card-border-hover: rgba(232,121,168,0.35)
--sakura:            #e879a8   /* same as accent — kept for explicit references */
--sakura-dim/glow/subtle  (slightly different opacities)
```

**Color philosophy**: violet is background-level only (body gradient, card-border, scrollbar, badge-violet). Sakura/pink is all interactive elements (buttons, inputs, progress bars, active states).

Component classes: `.card`, `.manga-card`, `.manga-card-title` (hover target), `.btn-primary`, `.btn-ghost`, `.input`, `.badge-green`, `.badge-violet`, `.badge-orange`.

Body: `#09090f` with dual-bloom radial gradient (violet left, sakura right).

---

## Data Flow — Chapter Translation

```
User pastes URL
  → POST /api/jobs/from-url
  → manga_downloader.py downloads pages → original/
  → SSE stream opened at /api/jobs/{id}/status
  → pipeline runs in background asyncio task:
      splitter  → original/
      detector  → detection/
      ocr       → detection/ (fills source_text)
      inpainter → cleaned/
      translator → detection/ (fills hebrew_text) + glossary.json
      typesetter → output/ + result.pdf
  → library.register_chapter() → SQLite / Supabase + optional R2 upload
  → SSE emits {stage: "library_ready", library_id: "..."}
  → Frontend redirects to /library/{id}
  → Reader fetches chapter metadata + serves pages
```

---

## Key Design Decisions

- **WeebCentral `mangadex_id`**: Stored as `wc:{ULID}` — the `wc:` prefix distinguishes it from real MangaDex UUIDs. Library lookup uses both chapter_num and the prefixed ULID as map keys.
- **Chapter batching**: MangaDex page shows 100 chapters at a time with labeled chip nav (shows actual chapter number ranges, not batch indices). `BATCH_SIZE = 100`, resets on filter/sort change.
- **SSE direct connect**: Browser always connects to FastAPI directly (not through Next.js proxy) to avoid Node.js fetch buffering that breaks streaming.
- **LTR zoom scrolling**: When zoom > 1, slide containers use `items-start` + `overflow-y: auto` so user can scroll from top to bottom of a zoomed page.
- **Progress bar tracking**: `targetPageRef` tracks where navigation is headed (for button disabled state). `IntersectionObserver` with `threshold: 0.5` tracks what's actually visible (for progress display). No seek gate — observer always runs.
- **Library modes**: Zero config = LOCAL (SQLite + disk). Add R2 vars = HYBRID (SQLite + CDN). Add Supabase vars = FULL CLOUD.
