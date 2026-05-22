# Frontend Architecture — Hebrew Manga Library

## Vision

Netflix-style web app. The homepage shows every manga that has at least one
Hebrew-translated chapter. Users browse, discover new titles via MangaDex search,
pick a chapter, and either read it (already translated, served from R2) or
trigger translation (pipeline runs → job progress → reader).

No user accounts. Everything is shared — translate once, read forever.

---

## Route Map

```
/                        Library homepage — grid of translated manga series
/discover                Browse / search MangaDex for new titles
/manga/[mangadex-id]     Manga detail — chapter list (read ✓ / translate ○)
/translate               Power-user URL / file upload form (moved from /)
/jobs/[id]               Translation pipeline progress  (existing)
/library/[chapter-id]    Web reader                     (existing)
```

---

## Page Breakdown

### `/` — Library

**Goal:** Show every manga series that has at least one Hebrew chapter, Netflix-style.

**Data:** `GET /api/library` → our backend → Supabase  
**No MangaDex calls** — all data already stored at translation time (cover URL, title, chapter list).

**Layout:**
```
┌─────────────────────────────────────────────────┐
│  NavBar: 📚 Hebrew Manga  |  Discover  |  Upload │
├─────────────────────────────────────────────────┤
│  Hero: "X series · Y chapters translated"        │
│  [Search translated titles…]                     │
├─────────────────────────────────────────────────┤
│  ┌────┐  ┌────┐  ┌────┐  ┌────┐  ┌────┐         │
│  │cover│  │cover│  │cover│  │cover│  │ + Add │   │
│  │    │  │    │  │    │  │    │  │ manga │   │
│  │Title│  │Title│  │Title│  │Title│  │      │   │
│  │2 ch │  │5 ch │  │1 ch │  │3 ch │         │   │
│  └────┘  └────┘  └────┘  └────┘  └────┘         │
└─────────────────────────────────────────────────┘
```
Click any card → `/manga/[mangadex-id]`

---

### `/discover` — Discover

**Goal:** Browse/search MangaDex to find a manga to translate.

**Data:**  
- Featured: `GET https://api.mangadex.org/manga?order[followedCount]=desc&includes[]=cover_art&limit=20`  
- Search: `GET https://api.mangadex.org/manga?title={q}&includes[]=cover_art&limit=20`  
- (Direct MangaDex API — CORS open, no backend proxy needed)

**Layout:**
```
┌─────────────────────────────────────────────────┐
│  NavBar                                          │
├─────────────────────────────────────────────────┤
│  🔍 [Search any manga…                      ]    │
├─────────────────────────────────────────────────┤
│  Popular on MangaDex          Already in Hebrew  │
│  ┌────┐  ┌────┐  ┌────┐      badge overlay ✓    │
│  │cover│  │cover│  │cover│                       │
│  │    │  │    │  │    │                         │
│  │Title│  │Title│  │Title│                       │
│  └────┘  └────┘  └────┘                         │
└─────────────────────────────────────────────────┘
```
Click any card → `/manga/[mangadex-id]`  
Cards with an existing Hebrew translation show a green "✓ In Library" badge.

---

### `/manga/[mangadex-id]` — Manga Detail

**Goal:** Show the manga's full chapter list. Each chapter is either "Read ✓" (translated) or "Translate →" (not yet).

**Data:**
- Manga details: `GET https://api.mangadex.org/manga/{id}?includes[]=cover_art&includes[]=author`
- Chapter list: `GET https://api.mangadex.org/manga/{id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100`
- Our translations: `GET /api/library/manga/{mangadex_manga_id}` → list of `{mangadex_chapter_id, library_id}`

**Layout:**
```
┌─────────────────────────────────────────────────┐
│  NavBar                                          │
├───────────┬─────────────────────────────────────┤
│  Cover    │  Manga Title                        │
│  image    │  Author · Status · Rating           │
│  (200px)  │  Description…                       │
│           │  [Browse on MangaDex ↗]             │
├───────────┴─────────────────────────────────────┤
│  Chapters                       Filter: [All ▾]  │
│  ─────────────────────────────────────────────  │
│  Ch. 1  The Death and the Strawberry   ✓ Read   │
│  Ch. 2  Transcript                     ✓ Read   │
│  Ch. 3  One-Sided Sympathy             ○ Translate│
│  Ch. 4  …                              ○ Translate│
└─────────────────────────────────────────────────┘
```
- ✓ Read → `/library/[chapter-id]`
- ○ Translate → POST `/api/jobs/from-url` → redirect to `/jobs/[id]`

---

### `/translate` — Upload (Power User)

The old homepage form, now at `/translate`.  
URL paste or file upload. Same behavior as before.

---

## Component Tree

```
app/
  layout.tsx                ← NavBar wraps every page
  page.tsx                  ← Library homepage
  discover/page.tsx         ← MangaDex browse/search
  manga/[id]/page.tsx       ← Chapter list
  translate/page.tsx        ← Old submit form (moved)
  jobs/[id]/page.tsx        ← Job progress  (exists)
  library/[id]/page.tsx     ← Web reader    (exists)

components/
  NavBar.tsx                ← Site-wide navigation
  MangaCard.tsx             ← Cover + title + badge (used on / and /discover)
  MangaCover.tsx            ← img with aspect ratio + fallback (reused in MangaCard + /manga/[id])
  Spinner.tsx               ← Single shared spinner
  SkeletonCard.tsx          ← Loading placeholder for manga cards
```

---

## Data Flow Diagram

```
Browser                   Our Backend              MangaDex API
───────                   ───────────              ────────────

Library page
  GET /api/library    →   Supabase query
                      ←   [{manga_title, cover_url, chapters…}]

Discover page
  GET /manga?…        ────────────────────────→   MangaDex search
                      ←────────────────────────   [{id, title, cover…}]
  GET /api/library    →   Supabase query
                      ←   (to mark "already in library")

Manga detail page
  GET /manga/{id}     ────────────────────────→   MangaDex detail
  GET /manga/{id}/feed────────────────────────→   MangaDex chapters
                      ←────────────────────────   chapter list
  GET /api/library/manga/{manga_id}  →  Supabase
                      ←   [{mangadex_id, library_id}]  (translated chapters)

Translate button
  POST /api/jobs/from-url  →  check cache → run pipeline
                           ←  {job_id} or {cached:true, library_id}
  → /jobs/[id]  or  → /library/[id]
```

---

## New Backend Endpoint Needed

```
GET /api/library/manga/{mangadex_manga_id}

Returns: [{ mangadex_id, id, chapter_num, chapter_title }]

Purpose: Manga detail page needs to know which specific chapters are
         already translated, keyed by mangadex chapter UUID.
         Much faster than loading all chapters and filtering client-side.
```

---

## MangaDex API Reference

All calls are made directly from the browser — MangaDex has open CORS.

| Purpose | Endpoint |
|---|---|
| Featured / popular | `GET /manga?order[followedCount]=desc&includes[]=cover_art&limit=20` |
| Search by title | `GET /manga?title={q}&includes[]=cover_art&limit=20` |
| Manga detail | `GET /manga/{id}?includes[]=cover_art&includes[]=author` |
| Chapter list | `GET /manga/{id}/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100` |

Cover URL: `https://uploads.mangadex.org/covers/{manga_id}/{cover_filename}.512.jpg`

---

## Build Steps

### Step 1 — Shared NavBar + layout
- Create `frontend/components/NavBar.tsx`
- Update `frontend/app/layout.tsx` to render `<NavBar>` above `{children}`
- Routes: Library (/) · Discover · Upload

### Step 2 — Shared components
- `MangaCover.tsx` — `<img>` with 3:4 aspect ratio, object-cover, emoji fallback
- `MangaCard.tsx` — cover + title + subtitle + optional badge, hover scale
- `SkeletonCard.tsx` — grey animated placeholder for loading state
- `Spinner.tsx` — deduplicate from existing pages

### Step 3 — Library homepage (`/`)
- Replace current submit form with Netflix grid
- Call `GET /api/library`, group by `manga_id`
- Render `<MangaCard>` for each series
- Add search/filter input (client-side filter on manga title)
- Empty state + "Find on MangaDex →" CTA

### Step 4 — Discover page (`/discover`)
- Search bar with 400ms debounce → MangaDex title search
- Below search: "Popular" grid using MangaDex followed-count sort
- Cross-reference `/api/library` to overlay "✓ In Library" badge
- Click → `/manga/[mangadex-id]`

### Step 5 — Manga detail page (`/manga/[id]`)
- Fetch manga info from MangaDex (cover, title, description, author)
- Fetch chapter feed from MangaDex
- Fetch `/api/library/manga/{id}` from our backend
- Render chapter list: "Read in Hebrew" vs "Translate" per chapter
- Translate click → POST job → redirect

### Step 6 — New backend endpoint
- `GET /api/library/manga/{mangadex_manga_id}` in `backend/main.py`
- Query Supabase: `chapters.select("id,mangadex_id,chapter_num,chapter_title").eq("manga_id", …)`

### Step 7 — Move submit form to `/translate`
- Create `frontend/app/translate/page.tsx` (copy current `page.tsx`)
- Delete old form content from `page.tsx`
- Update NavBar "Upload" link → `/translate`

### Step 8 — Wire job-completion → library
- After `/jobs/[id]` reaches `done`, show "View in Library →" button if `library_id` is emitted
- Backend already fires `_register_in_library` after pipeline — emit `library_id` in done event
