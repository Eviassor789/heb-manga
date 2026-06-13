'use client'

import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { cacheGet, cacheSet } from '@/lib/cache'
import { getGeminiKey, getModalTokens, getApiHeaders } from '@/lib/apiKeys'

// ── Types ──────────────────────────────────────────────────────────────────────

type ReadDirection  = 'ttb' | 'ltr'
type EndCardState  = 'loading' | 'translated' | 'untranslated' | 'caught-up'

interface Chapter {
  id:            string
  manga_title:   string
  manga_id:      string | null
  mangadex_id:   string | null
  chapter_num:   string | null
  chapter_title: string | null
  cover_url:     string | null
  page_count:    number | null
  pdf_url:       string | null
  pages_prefix:  string | null
  pdf_size_kb:   number | null
  translated_at: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function pageUrl(prefix: string, idx: number): string {
  return `${prefix}/${String(idx).padStart(3, '0')}.jpg`
}

/** Navigate back to the series page this chapter belongs to. */
function getBackUrl(chapter: Chapter): string {
  if (!chapter.manga_id) return '/'
  if (chapter.mangadex_id?.startsWith('wc:')) {
    // WeebCentral chapter — manga_id is the WC series ULID
    return `/weebcentral/${chapter.manga_id}`
  }
  // MangaDex chapter — manga_id is the MangaDex manga UUID
  if (chapter.manga_id) return `/manga/${chapter.manga_id}`
  return '/'
}

// ── Constants ──────────────────────────────────────────────────────────────────

const ZOOM_STEP = 0.25
const ZOOM_MIN  = 0.5
const ZOOM_MAX  = 3.0
const ZOOM_DEFAULT = 1.0

// Base max-width for the TTB strip at zoom 1.0 (matches Tailwind max-w-3xl)
const TTB_BASE_WIDTH_PX = 768

// ── Component ──────────────────────────────────────────────────────────────────

export default function ReaderPage() {
  const { id }  = useParams<{ id: string }>()
  const router  = useRouter()

  const [chapter,      setChapter]      = useState<Chapter | null>(null)
  const [loading,      setLoading]      = useState(true)
  const [error,        setError]        = useState<string | null>(null)
  const [currentPage,  setCurrentPage]  = useState(1)
  const [hoverPage,    setHoverPage]    = useState<number | null>(null)
  const [hoverX,       setHoverX]       = useState(0)
  const [barVisible,   setBarVisible]   = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [direction,    setDirection]    = useState<ReadDirection>('ttb')
  const [zoom,         setZoom]         = useState(ZOOM_DEFAULT)

  // ── End-of-chapter card state ─────────────────────────────────────────────
  const [endCardState,     setEndCardState]     = useState<EndCardState>('loading')
  const [nextChapter,      setNextChapter]      = useState<Chapter | null>(null)
  const [nextChapterUrl,   setNextChapterUrl]   = useState<string | null>(null)
  const [nextChapterLabel, setNextChapterLabel] = useState<string>('')
  const [translating,      setTranslating]      = useState(false)

  const pageRefs        = useRef<(HTMLDivElement | null)[]>([])
  // Refs for individual slides in LTR mode — used to reset scrollTop on page change.
  const ltrSlideRefs    = useRef<(HTMLDivElement | null)[]>([])
  const seekingRef      = useRef(false)
  const seekTimerRef    = useRef<ReturnType<typeof setTimeout> | null>(null)
  const hideTimerRef    = useRef<ReturnType<typeof setTimeout> | null>(null)
  const barVisibleRef   = useRef(false)
  const settingsRef     = useRef(false)
  // Saved page number read from localStorage on mount; consumed once after chapter loads.
  const initialPageRef  = useRef(0)
  // Refs for the settings popover and the gear toggle button (click-outside detection).
  const settingsPanelRef = useRef<HTMLDivElement>(null)
  const gearButtonRef    = useRef<HTMLButtonElement>(null)
  const touchStartXRef   = useRef<number>(0)
  const touchStartYRef   = useRef<number>(0)
  // Set to true by changeZoom so the layout effect knows to snap scroll position.
  const hasZoomedRef    = useRef(false)
  // Mirrors currentPage as a ref so useLayoutEffect can read it without deps.
  const currentPageRef  = useRef(1)

  useEffect(() => { settingsRef.current = settingsOpen }, [settingsOpen])

  // Close settings when the user clicks anywhere outside the panel or gear button.
  useEffect(() => {
    if (!settingsOpen) return
    const handleDown = (e: MouseEvent) => {
      if (
        settingsPanelRef.current?.contains(e.target as Node) ||
        gearButtonRef.current?.contains(e.target as Node)
      ) return
      setSettingsOpen(false)
    }
    document.addEventListener('mousedown', handleDown)
    return () => document.removeEventListener('mousedown', handleDown)
  }, [settingsOpen])


  // ── Load preferences + saved page ────────────────────────────────────────

  useEffect(() => {
    const savedDir  = localStorage.getItem('reader-direction') as ReadDirection | null
    const savedZoom = parseFloat(localStorage.getItem('reader-zoom') ?? '')

    if (savedDir && ['ttb', 'ltr'].includes(savedDir)) setDirection(savedDir)
    if (!isNaN(savedZoom)) setZoom(Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, savedZoom)))

    // Read saved page for this chapter — applied after chapter metadata loads.
    if (id) {
      const saved = parseInt(localStorage.getItem(`hemanga-page-${id}`) ?? '', 10)
      if (saved > 1) initialPageRef.current = saved
    }
  }, [id])

  const changeDirection = useCallback((d: ReadDirection) => {
    setDirection(d)
    localStorage.setItem('reader-direction', d)
    setSettingsOpen(false)
    setCurrentPage(1)
    if (d === 'ttb') {
      requestAnimationFrame(() => {
        const el = pageRefs.current[0]
        if (el) el.scrollIntoView({ behavior: 'instant', block: 'start' })
      })
    }
  }, [])

  // Keep currentPageRef in sync so the layout effect below can read it.
  useEffect(() => { currentPageRef.current = currentPage }, [currentPage])

  const changeZoom = useCallback((z: number) => {
    const clamped = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX,
      Math.round(z / ZOOM_STEP) * ZOOM_STEP,
    ))
    hasZoomedRef.current = true
    setZoom(clamped)
    localStorage.setItem('reader-zoom', String(clamped))
  }, [])

  // After a zoom change reflows the layout, snap the current page back into view.
  // TTB: scrollIntoView snaps the page to the top.
  // LTR: reset the slide's internal scrollTop so the user starts at the top of
  //       the (now larger/smaller) page rather than being stuck in the middle.
  useLayoutEffect(() => {
    if (!hasZoomedRef.current) return
    hasZoomedRef.current = false
    const ttbEl = pageRefs.current[currentPageRef.current - 1]
    if (ttbEl) { ttbEl.scrollIntoView({ behavior: 'instant', block: 'start' }); return }
    const ltrEl = ltrSlideRefs.current[currentPageRef.current - 1]
    if (ltrEl) ltrEl.scrollTop = 0
  }, [zoom])

  // When navigating to a different page in LTR mode, reset the slide's scroll
  // position so the user always starts at the top of the new page.
  useLayoutEffect(() => {
    const el = ltrSlideRefs.current[currentPage - 1]
    if (el) el.scrollTop = 0
  }, [currentPage])

  // ── Fetch chapter metadata (cached 30 min — immutable after translation) ───

  useEffect(() => {
    if (!id) return
    const chKey = `reader:chapter:${id}`
    const cached = cacheGet<Chapter>(chKey, 30 * 60_000)
    if (cached) { setChapter(cached); setLoading(false); return }

    fetch(`/api/library/${id}`)
      .then(r => { if (!r.ok) throw new Error('Chapter not found'); return r.json() })
      .then(data => { cacheSet(chKey, data); setChapter(data); setLoading(false) })
      .catch(err  => { setError(err.message); setLoading(false) })
  }, [id])

  // ── Discover next chapter (translated / untranslated / caught-up) ───────────

  useEffect(() => {
    if (!chapter) return

    const currentNum = parseFloat(chapter.chapter_num ?? '')
    if (!chapter.manga_id || isNaN(currentNum)) {
      setEndCardState('caught-up')
      return
    }

    let cancelled = false

    ;(async () => {
      // Step 1 — check our own library for an already-translated next chapter
      try {
        const r = await fetch(`/api/library/manga/${chapter.manga_id}`)
        if (cancelled) return
        const { chapters: libChapters } = r.ok
          ? (await r.json() as { chapters: Chapter[] })
          : { chapters: [] as Chapter[] }

        const sorted = libChapters
          .filter(c => c.id !== id && !isNaN(parseFloat(c.chapter_num ?? '')))
          .sort((a, b) => parseFloat(a.chapter_num!) - parseFloat(b.chapter_num!))
        const next = sorted.find(c => parseFloat(c.chapter_num!) > currentNum)

        if (next) {
          if (cancelled) return
          setNextChapter(next)
          setNextChapterLabel(`Chapter ${next.chapter_num}`)
          setEndCardState('translated')
          return
        }
      } catch { /* fall through */ }

      if (cancelled) return

      // Step 2 — check the source platform for an untranslated next chapter
      try {
        if (chapter.mangadex_id?.startsWith('wc:')) {
          // WeebCentral
          const r = await fetch(`/api/weebcentral/series/${chapter.manga_id}/chapters`)
          if (cancelled) return
          if (!r.ok) throw new Error()
          const { chapters: wcChapters } = await r.json() as {
            chapters: Array<{ id: string; number: string; title: string; url: string }>
          }
          const wcSorted = wcChapters
            .filter(c => !isNaN(parseFloat(c.number)))
            .sort((a, b) => parseFloat(a.number) - parseFloat(b.number))
          const wcNext = wcSorted.find(c => parseFloat(c.number) > currentNum)
          if (wcNext) {
            if (cancelled) return
            setNextChapterUrl(wcNext.url)
            setNextChapterLabel(`Chapter ${wcNext.number}`)
            setEndCardState('untranslated')
            return
          }
        } else {
          // MangaDex — query the chapter feed for anything after currentNum
          const params = new URLSearchParams({
            limit: '1',
            'order[chapter]': 'asc',
            'translatedLanguage[]': 'en',
            chapter: String(currentNum + 0.0001),
          })
          const r = await fetch(
            `https://api.mangadex.org/manga/${chapter.manga_id}/feed?${params}`
          )
          if (cancelled) return
          if (!r.ok) throw new Error()
          const data = await r.json()
          if (data.data?.length > 0) {
            const mdNext   = data.data[0]
            const mdNum    = mdNext.attributes?.chapter ?? ''
            setNextChapterUrl(`https://mangadex.org/chapter/${mdNext.id}`)
            setNextChapterLabel(`Chapter ${mdNum}`)
            setEndCardState('untranslated')
            return
          }
        }
      } catch { /* fall through to caught-up */ }

      if (!cancelled) setEndCardState('caught-up')
    })()

    return () => { cancelled = true }
  }, [chapter, id])

  // ── Persist reading progress to localStorage (for "Continue Reading" row) ──

  useEffect(() => {
    if (!chapter || !id) return
    try {
      // Save per-chapter page number so the reader can resume at the right spot.
      localStorage.setItem(`hemanga-page-${id}`, String(currentPage))

      const entry = {
        manga_id:    chapter.manga_id ?? id,
        manga_title: chapter.manga_title,
        cover_url:   chapter.cover_url ?? null,
        chapter_id:  id,
        chapter_num: chapter.chapter_num ?? null,
        // Cap at page_count so the end-card slide index is never persisted.
        page_num:    Math.min(currentPage, chapter.page_count ?? currentPage),
        page_count:  chapter.page_count ?? null,
        last_read:   new Date().toISOString(),
      }
      const raw      = localStorage.getItem('hemanga-continue-reading') ?? '[]'
      const prev     = JSON.parse(raw) as typeof entry[]
      const filtered = prev.filter(e => e.manga_id !== entry.manga_id)
      localStorage.setItem(
        'hemanga-continue-reading',
        JSON.stringify([entry, ...filtered].slice(0, 20)),
      )
    } catch { /* localStorage unavailable — ignore */ }
  }, [currentPage, chapter, id])

  // ── Restore saved page position after chapter metadata loads ─────────────
  // initialPageRef is set from localStorage on mount; consumed here exactly once.
  // direction is already set from localStorage (effect above runs before fetch completes).

  useEffect(() => {
    const target = initialPageRef.current
    if (!target || !chapter?.page_count) return
    initialPageRef.current = 0   // consume — don't restore again on re-renders

    const clamped = Math.min(target, chapter.page_count)
    if (clamped <= 1) return

    setCurrentPage(clamped)

    if (direction === 'ttb') {
      // Images may still be loading so we wait a short moment before scrolling,
      // giving the browser time to assign heights to the page divs.
      setTimeout(() => {
        const el = pageRefs.current[clamped - 1]
        if (el) el.scrollIntoView({ behavior: 'instant', block: 'start' })
      }, 120)
    }
    // LTR: setCurrentPage above is sufficient — the slide strip translates immediately.
  }, [chapter, direction])

  // ── Intersection observer → track visible page (TTB only) ─────────────────

  useEffect(() => {
    if (!chapter?.page_count || direction !== 'ttb') return
    const obs = new IntersectionObserver(
      entries => {
        if (seekingRef.current) return
        for (const entry of entries) {
          // Ignore 0-height elements — lazy images haven't loaded yet and
          // some browsers treat them as "fully intersecting" (0% outside = 100% in).
          if (entry.isIntersecting && entry.boundingClientRect.height > 0) {
            const idx = Number((entry.target as HTMLElement).dataset.page)
            if (!isNaN(idx)) setCurrentPage(idx)
          }
        }
      },
      { threshold: 0.4 },
    )
    pageRefs.current.forEach(el => el && obs.observe(el))
    return () => obs.disconnect()
  // chapter?.page_count changing means a new chapter loaded, which also means
  // the end-card ref (pageRefs[pageCount]) has been added — re-observe everything.
  }, [chapter?.page_count, direction])

  // ── Scroll to page (TTB) ───────────────────────────────────────────────────

  const scrollToPage = useCallback((n: number) => {
    const el = pageRefs.current[n - 1]
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // ── Seek ──────────────────────────────────────────────────────────────────

  const seekTo = useCallback((targetPage: number) => {
    seekingRef.current = true
    setCurrentPage(targetPage)
    if (direction === 'ttb') scrollToPage(targetPage)
    if (seekTimerRef.current) clearTimeout(seekTimerRef.current)
    seekTimerRef.current = setTimeout(() => { seekingRef.current = false }, 750)
  }, [direction, scrollToPage])

  // ── Trigger translation of the next (untranslated) chapter ──────────────────

  const translateNext = useCallback(async () => {
    if (!nextChapterUrl || translating) return
    setTranslating(true)
    try {
      const body: Record<string, string> = { url: nextChapterUrl }
      const geminiKey = getGeminiKey()
      const { tokenId, tokenSecret } = getModalTokens()
      if (geminiKey)              body.gemini_api_key    = geminiKey
      if (tokenId && tokenSecret) { body.modal_token_id = tokenId; body.modal_token_secret = tokenSecret }

      const res = await fetch('/api/jobs/from-url', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', ...getApiHeaders() },
        body:    JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      router.push(data.cached && data.library_id ? `/library/${data.library_id}` : `/jobs/${data.job_id}`)
    } catch {
      setTranslating(false)
    }
  }, [nextChapterUrl, translating, router])

  // ── Keyboard navigation ────────────────────────────────────────────────────

  useEffect(() => {
    const count = chapter?.page_count ?? 0
    // maxNav = pageCount + 1 so arrow keys can reach the end card in both modes.
    const maxNav = count > 0 ? count + 1 : count
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown')
        seekTo(Math.min(currentPage + 1, maxNav))
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp')
        seekTo(Math.max(currentPage - 1, 1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [chapter?.page_count, currentPage, seekTo])

  // ── Bottom bar auto-show/hide via mouse proximity ─────────────────────────

  const showBar = useCallback(() => {
    if (hideTimerRef.current) { clearTimeout(hideTimerRef.current); hideTimerRef.current = null }
    if (!barVisibleRef.current) { barVisibleRef.current = true; setBarVisible(true) }
  }, [])

  const scheduleHide = useCallback((ms = 1500) => {
    if (hideTimerRef.current) return
    hideTimerRef.current = setTimeout(() => {
      hideTimerRef.current = null
      if (settingsRef.current) return
      barVisibleRef.current = false
      setBarVisible(false)
      setHoverPage(null)
    }, ms)
  }, [])

  useEffect(() => {
    let wasNear = false
    // Local vars to track the touch start position for tap vs. scroll detection.
    // Using local vars (not refs) is safe here because the closure is recreated
    // whenever showBar/scheduleHide change (the useEffect deps).
    let barTouchStartX = 0
    let barTouchStartY = 0

    const onMove = (e: MouseEvent) => {
      const near = window.innerHeight - e.clientY < 120
      if (near && !wasNear) showBar()
      else if (!near && wasNear) scheduleHide()
      wasNear = near
    }

    // Record position on touchstart — don't show bar yet.
    // Showing on touchstart causes flashing on every scroll gesture.
    const onTouchStart = (e: TouchEvent) => {
      barTouchStartX = e.touches[0].clientX
      barTouchStartY = e.touches[0].clientY
    }

    // Show bar only if the touch ended with minimal movement (i.e. a tap, not a scroll/swipe).
    const onTouchEnd = (e: TouchEvent) => {
      const dx = Math.abs(e.changedTouches[0].clientX - barTouchStartX)
      const dy = Math.abs(e.changedTouches[0].clientY - barTouchStartY)
      if (dx < 10 && dy < 10) {
        showBar()
        scheduleHide(3000)   // stay visible for 3 s on mobile tap
      }
    }

    window.addEventListener('mousemove',  onMove,      { passive: true })
    window.addEventListener('touchstart', onTouchStart, { passive: true })
    window.addEventListener('touchend',   onTouchEnd,   { passive: true })
    return () => {
      window.removeEventListener('mousemove',  onMove)
      window.removeEventListener('touchstart', onTouchStart)
      window.removeEventListener('touchend',   onTouchEnd)
    }
  }, [showBar, scheduleHide])

  // ── Segment bar hover ──────────────────────────────────────────────────────

  // handleBarMouseMove is defined after the chapter/maxPage are available in the
  // render body; we forward the live maxPage value via a ref so the callback
  // never becomes stale while still being stable across renders.
  const maxPageRef = useRef(0)

  const handleBarMouseMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (!maxPageRef.current) return
    const { left, width } = e.currentTarget.getBoundingClientRect()
    const pct  = Math.max(0, Math.min(1, (e.clientX - left) / width))
    const page = Math.max(1, Math.min(maxPageRef.current, Math.ceil(pct * maxPageRef.current) || 1))
    setHoverPage(page)
    setHoverX(e.clientX - left)
  }, [])

  // ── Render: loading / error ────────────────────────────────────────────────

  if (loading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-zinc-950">
        <ReaderSpinner />
      </main>
    )
  }

  if (error || !chapter) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center gap-4 bg-zinc-950">
        <p className="text-red-400">{error ?? 'Chapter not found'}</p>
        <button onClick={() => router.push('/')} className="btn-ghost">← Library</button>
      </main>
    )
  }

  const pageCount    = chapter.page_count ?? 0
  const pagesPrefix  = chapter.pages_prefix
  const chapterLabel = chapter.chapter_num
    ? `Ch. ${chapter.chapter_num}${chapter.chapter_title ? ` — ${chapter.chapter_title}` : ''}`
    : chapter.chapter_title ?? ''
  const backUrl      = getBackUrl(chapter)

  // The end card is always page (pageCount + 1) — reachable in both TTB and LTR.
  const maxPage = pageCount > 0 ? pageCount + 1 : pageCount
  // Keep the bar-scrubber callback up-to-date without recreating it every render.
  maxPageRef.current = maxPage
  // Counter text still shows "5 / 5" not "6 / 5" when on the end card.
  const displayPage = Math.min(currentPage, pageCount)

  // ── Horizontal single-page viewer ─────────────────────────────────────────

  const renderHorizontal = () => (
    <div
      className="relative w-full h-screen overflow-hidden bg-zinc-950"
      onTouchStart={e => {
        touchStartXRef.current = e.touches[0].clientX
        touchStartYRef.current = e.touches[0].clientY
      }}
      onTouchEnd={e => {
        const dx = e.changedTouches[0].clientX - touchStartXRef.current
        const dy = e.changedTouches[0].clientY - touchStartYRef.current
        // Only handle horizontal swipes (|dx| > |dy| and |dx| > 40px threshold)
        if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 40) {
          if (dx < 0) seekTo(Math.min(currentPage + 1, maxPage))  // swipe left → next
          else        seekTo(Math.max(currentPage - 1, 1))          // swipe right → prev
        }
      }}
    >

      {/* Sliding strip */}
      <div
        className="flex h-full transition-transform duration-300 ease-in-out"
        style={{ transform: `translateX(${-(currentPage - 1) * 100}%)` }}
      >
        {pagesPrefix && pageCount > 0 ? (
          <>
            {Array.from({ length: pageCount }, (_, i) => i + 1).map(n => (
              <div
                key={n}
                ref={el => { ltrSlideRefs.current[n - 1] = el }}
                className={`flex-shrink-0 flex justify-center bg-zinc-950 ${zoom > 1 ? 'items-start' : 'items-center'}`}
                style={{
                  width:     '100vw',
                  height:    '100%',
                  minWidth:  '100vw',
                  overflowX: 'hidden',
                  overflowY: zoom > 1 ? 'auto' : 'hidden',
                }}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={pageUrl(pagesPrefix, n)}
                  alt={`Page ${n}`}
                  className="select-none"
                  style={{
                    width:         `${zoom * 100}%`,
                    height:        `${zoom * 100}%`,
                    objectFit:     'contain',
                    flexShrink:    0,
                    paddingBottom: '3.5rem',
                  }}
                  loading={Math.abs(n - currentPage) <= 1 ? 'eager' : 'lazy'}
                  decoding="async"
                  draggable={false}
                />
              </div>
            ))}

            {/* End-of-chapter card — final slide, portrait box centred in the slide */}
            <div
              key="end-card"
              className="flex-shrink-0 flex items-center justify-center bg-zinc-950"
              style={{ width: '100vw', height: '100%', minWidth: '100vw' }}
            >
              {/* Portrait page-like box — same feel as a real manga page */}
              <div
                className="flex items-center justify-center overflow-y-auto"
                style={{
                  // Height = full slide minus bottom bar; width = portrait ratio of that height
                  height:     'calc(100% - 3.5rem)',
                  width:      'calc((100% - 3.5rem) * 0.714)',  // 1/1.4 ≈ portrait ratio
                  maxWidth:   '90vw',
                  background: 'rgba(14, 14, 22, 0.95)',
                  border:     '1px solid rgba(255,255,255,0.07)',
                }}
              >
                <EndCard
                  chapter={chapter}
                  endCardState={endCardState}
                  nextChapter={nextChapter}
                  nextChapterLabel={nextChapterLabel}
                  translating={translating}
                  onTranslate={translateNext}
                  backUrl={backUrl}
                  onBack={() => router.push(backUrl)}
                />
              </div>
            </div>
          </>
        ) : (
          <div
            className="flex-shrink-0 flex items-center justify-center"
            style={{ width: '100vw', height: '100%', minWidth: '100vw' }}
          >
            <div className="text-center space-y-4">
              <p className="text-zinc-500 text-sm">Page images not available.</p>
              <button onClick={() => router.push('/')} className="btn-ghost">← Library</button>
            </div>
          </div>
        )}
      </div>

      {/* Left / right click zones */}
      {pageCount > 1 && (
        <>
          <div
            className="absolute left-0 top-0 h-full z-10 cursor-pointer"
            style={{ width: '15%', bottom: '3.5rem' }}
            onClick={() => seekTo(Math.max(currentPage - 1, 1))}
            aria-label="Previous page"
          />
          <div
            className="absolute right-0 top-0 h-full z-10 cursor-pointer"
            style={{ width: '15%', bottom: '3.5rem' }}
            onClick={() => seekTo(Math.min(currentPage + 1, maxPage))}
            aria-label="Next page"
          />
        </>
      )}
    </div>
  )

  // ── Vertical strip (TTB) ──────────────────────────────────────────────────

  const renderVertical = () => (
    <div className="pb-6">
      {pagesPrefix && pageCount > 0 ? (
        <>
          {Array.from({ length: pageCount }, (_, i) => i + 1).map(n => (
            <div
              key={n}
              ref={el => { pageRefs.current[n - 1] = el }}
              data-page={n}
              className="mx-auto mb-1"
              style={{ maxWidth: `${Math.round(TTB_BASE_WIDTH_PX * zoom)}px` }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={pageUrl(pagesPrefix, n)}
                alt={`Page ${n}`}
                className="w-full block"
                style={{ aspectRatio: '2/3' }}
                loading={n <= 3 ? 'eager' : 'lazy'}
                decoding="async"
                onError={e => {
                  const img = e.target as HTMLImageElement
                  img.style.display = 'none'
                  img.nextElementSibling?.classList.remove('hidden')
                }}
              />
              <div className="hidden bg-zinc-900 border border-zinc-800 rounded-lg py-12 text-center text-zinc-600 text-sm">
                Page {n} unavailable
              </div>
            </div>
          ))}

          {/* End-of-chapter card — registered as page (pageCount+1) in the observer */}
          <div
            ref={el => { pageRefs.current[pageCount] = el }}
            data-page={pageCount + 1}
            className="mx-auto mb-1 flex items-center justify-center"
            style={{
              maxWidth:   `${Math.round(TTB_BASE_WIDTH_PX * zoom)}px`,
              minHeight:  `${Math.round(TTB_BASE_WIDTH_PX * zoom * 1.4)}px`,
              background: 'rgba(14, 14, 22, 0.95)',
              border:     '1px solid rgba(255,255,255,0.07)',
            }}
          >
            <EndCard
              chapter={chapter}
              endCardState={endCardState}
              nextChapter={nextChapter}
              nextChapterLabel={nextChapterLabel}
              translating={translating}
              onTranslate={translateNext}
              backUrl={backUrl}
              onBack={() => router.push(backUrl)}
            />
          </div>
        </>
      ) : (
        <div className="max-w-3xl mx-auto pt-32 text-center space-y-4">
          <p className="text-zinc-500 text-sm">Page images not available.</p>
          <button onClick={() => router.push('/')} className="btn-ghost">← Library</button>
        </div>
      )}
    </div>
  )

  // ── Main render ───────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-zinc-950">

      {direction === 'ttb' ? renderVertical() : renderHorizontal()}

      {/* ── Bottom bar ─────────────────────────────────────────────────── */}
      <div className="fixed bottom-0 left-0 right-0 z-50">

        {/* Settings popover */}
        {settingsOpen && (
          <div
            ref={settingsPanelRef}
            className="absolute right-2 rounded-xl overflow-hidden"
            style={{
              bottom:         'calc(100% + 0.5rem)',
              background:     'rgba(9,9,15,0.97)',
              border:         '1px solid var(--card-border-hover)',
              backdropFilter: 'blur(16px)',
              boxShadow:      '0 8px 32px rgba(0,0,0,0.7)',
              minWidth:       '13rem',
            }}
          >
            <div className="px-4 py-3 space-y-4">

              {/* Direction */}
              <div>
                <p className="text-[10px] font-bold text-zinc-500 uppercase tracking-widest mb-2">
                  Reading Direction
                </p>
                <div className="flex flex-col gap-1">
                  {([
                    ['ttb', '↕', 'Top to Bottom'],
                    ['ltr', '→', 'Left to Right'],
                  ] as [ReadDirection, string, string][]).map(([d, icon, label]) => (
                    <button
                      key={d}
                      onClick={() => changeDirection(d)}
                      className="flex items-center gap-2.5 w-full text-left px-3 py-2 rounded-lg text-sm transition-all"
                      style={{
                        background: direction === d ? 'var(--accent-subtle)' : 'transparent',
                        color:      direction === d ? 'var(--accent)' : '#a1a1aa',
                        border:     direction === d ? '1px solid var(--card-border-hover)' : '1px solid transparent',
                      }}
                    >
                      <span className="font-mono w-4 text-center">{icon}</span>
                      <span className="flex-1">{label}</span>
                      {direction === d && <span className="text-[10px]">✓</span>}
                    </button>
                  ))}
                </div>
              </div>

              {/* Zoom */}
              <div>
                <p className="text-[10px] font-bold text-zinc-500 uppercase tracking-widest mb-2">
                  Zoom
                </p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => changeZoom(zoom - ZOOM_STEP)}
                    disabled={zoom <= ZOOM_MIN}
                    className="w-8 h-8 rounded-lg flex items-center justify-center text-zinc-400 hover:text-zinc-100 transition-colors disabled:opacity-25 disabled:cursor-default text-lg font-light"
                    style={{ border: '1px solid var(--card-border-hover)', background: 'var(--card-bg)' }}
                    aria-label="Zoom out"
                  >
                    −
                  </button>

                  <span
                    className="flex-1 text-center text-sm tabular-nums font-mono"
                    style={{ color: 'var(--accent)' }}
                  >
                    {Math.round(zoom * 100)}%
                  </span>

                  <button
                    onClick={() => changeZoom(zoom + ZOOM_STEP)}
                    disabled={zoom >= ZOOM_MAX}
                    className="w-8 h-8 rounded-lg flex items-center justify-center text-zinc-400 hover:text-zinc-100 transition-colors disabled:opacity-25 disabled:cursor-default text-lg font-light"
                    style={{ border: '1px solid var(--card-border-hover)', background: 'var(--card-bg)' }}
                    aria-label="Zoom in"
                  >
                    +
                  </button>
                </div>
              </div>

            </div>
          </div>
        )}

        {/* ── Progress segments — thin strip always visible ─────────────── */}
        {pageCount > 0 && (
          <div
            className="relative flex items-end gap-[2px] px-1 cursor-pointer transition-all duration-200"
            style={{ height: barVisible ? '2rem' : '0.5rem' }}
            onMouseMove={barVisible ? handleBarMouseMove : undefined}
            onMouseLeave={() => setHoverPage(null)}
            onClick={() => { if (barVisible && hoverPage !== null) seekTo(hoverPage) }}
            onTouchMove={e => {
              if (!maxPageRef.current) return
              const touch = e.touches[0]
              const rect  = e.currentTarget.getBoundingClientRect()
              const pct   = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width))
              const page  = Math.max(1, Math.min(maxPageRef.current, Math.ceil(pct * maxPageRef.current) || 1))
              setHoverPage(page)
              setHoverX(touch.clientX - rect.left)
            }}
            onTouchEnd={() => { if (hoverPage !== null) seekTo(hoverPage) }}
            aria-label="Page navigation scrubber"
          >
            {/* Tooltip */}
            {barVisible && hoverPage !== null && (
              <div
                className="absolute bottom-full mb-1.5 px-2 py-0.5 rounded text-xs font-semibold pointer-events-none whitespace-nowrap"
                style={{
                  left:       `clamp(1.5rem, ${hoverX}px, calc(100% - 1.5rem))`,
                  transform:  'translateX(-50%)',
                  background: 'rgba(14,14,22,0.97)',
                  border:     '1px solid var(--card-border-hover)',
                  color:      'var(--accent)',
                  boxShadow:  '0 2px 8px rgba(0,0,0,0.5)',
                }}
              >
                {hoverPage > pageCount ? 'End' : hoverPage}
              </div>
            )}

            {/* Segments — pageCount real pages + 1 end-card segment, all uniform */}
            {Array.from({ length: maxPage }, (_, i) => {
              const page      = i + 1
              const isRead    = page <= currentPage
              const isHovered = barVisible && page === hoverPage
              return (
                <div
                  key={i}
                  className="flex-1 rounded-[1px] transition-all duration-100"
                  style={{
                    minWidth:  0,
                    height:    isHovered ? '10px' : barVisible ? '4px' : '3px',
                    background: isRead ? 'var(--accent)' : 'rgba(63,63,70,0.7)',
                    boxShadow: isRead && isHovered ? '0 0 6px var(--accent-glow)' : undefined,
                  }}
                />
              )
            })}
          </div>
        )}

        {/* ── Controls row ─────────────────────────────────────────────── */}
        <div
          className="overflow-hidden transition-all duration-200 ease-in-out"
          style={{ maxHeight: barVisible ? '5rem' : '0px', opacity: barVisible ? 1 : 0 }}
        >
          <div
            className="px-3 py-3 flex items-center gap-2"
            style={{
              background:     'rgba(9,9,15,0.96)',
              backdropFilter: 'blur(12px)',
              borderTop:      '1px solid rgba(139,92,246,0.12)',
            }}
          >
            {/* Back to series page */}
            <button
              onClick={() => router.push(backUrl)}
              className="shrink-0 text-zinc-500 hover:text-zinc-200 text-xs transition-colors flex items-center gap-1 mr-2"
            >
              ← <span className="hidden sm:inline">Back</span>
            </button>

            {/* Title + chapter */}
            <div className="flex-1 min-w-0">
              <p className="text-xs font-semibold text-zinc-100 truncate leading-tight">
                {chapter.manga_title}
              </p>
              {chapterLabel && (
                <p className="text-[11px] truncate leading-tight" style={{ color: 'var(--pink-soft)' }}>
                  {chapterLabel}
                </p>
              )}
            </div>

            {/* Prev / counter / next */}
            <div className="shrink-0 flex items-center gap-1.5">
              <button
                onClick={() => seekTo(Math.max(currentPage - 1, 1))}
                disabled={currentPage <= 1}
                className="w-9 h-9 rounded-lg flex items-center justify-center text-zinc-400 hover:text-zinc-100 transition-colors disabled:opacity-25 disabled:cursor-default text-sm"
                style={{ border: '1px solid rgba(139,92,246,0.2)' }}
                aria-label="Previous page"
              >
                ←
              </button>

              <span className="text-xs tabular-nums font-mono text-zinc-300 min-w-[3rem] text-center">
                {displayPage}<span className="text-zinc-600"> / </span>{pageCount}
              </span>

              <button
                onClick={() => seekTo(Math.min(currentPage + 1, maxPage))}
                disabled={currentPage >= maxPage}
                className="w-9 h-9 rounded-lg flex items-center justify-center text-zinc-400 hover:text-zinc-100 transition-colors disabled:opacity-25 disabled:cursor-default text-sm"
                style={{ border: '1px solid rgba(139,92,246,0.2)' }}
                aria-label="Next page"
              >
                →
              </button>
            </div>

            {/* Settings gear */}
            <button
              ref={gearButtonRef}
              onClick={e => { e.stopPropagation(); setSettingsOpen(o => !o) }}
              className="shrink-0 ml-2 w-9 h-9 rounded-lg flex items-center justify-center transition-all"
              style={{
                border:     '1px solid rgba(139,92,246,0.2)',
                color:      settingsOpen ? 'var(--accent)' : '#71717a',
                background: settingsOpen ? 'var(--accent-subtle)' : 'transparent',
              }}
              aria-label="Reader settings"
              title="Reading direction"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" />
              </svg>
            </button>
          </div>
        </div>

      </div>
    </div>
  )
}

// ── End-of-chapter card ───────────────────────────────────────────────────────

function EndCard({
  chapter,
  endCardState,
  nextChapter,
  nextChapterLabel,
  translating,
  onTranslate,
  onBack,
}: {
  chapter:          Chapter
  endCardState:     EndCardState
  nextChapter:      Chapter | null
  nextChapterLabel: string
  translating:      boolean
  onTranslate:      () => void
  backUrl:          string   // kept for the parent's convenience; onBack wraps it
  onBack:           () => void
}) {
  const chapterLabel = chapter.chapter_num
    ? `Chapter ${chapter.chapter_num}${chapter.chapter_title ? ` — ${chapter.chapter_title}` : ''}`
    : chapter.chapter_title ?? 'Chapter'

  return (
    <div className="flex flex-col items-center text-center px-8 gap-7 max-w-xs w-full py-16">

        {/* Completion badge */}
        <div
          className="w-20 h-20 rounded-full flex items-center justify-center"
          style={{
            background: 'var(--accent-subtle)',
            border:     '1.5px solid var(--card-border-hover)',
            boxShadow:  '0 0 48px var(--accent-glow)',
          }}
        >
          <svg width="30" height="30" viewBox="0 0 24 24" fill="none"
            stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>

        {/* Title block */}
        <div className="space-y-1">
          <p className="text-xs font-semibold tracking-wide" style={{ color: 'var(--accent)' }}>
            {chapterLabel} Complete
          </p>
          <p className="text-2xl font-bold text-zinc-100 leading-snug">
            {chapter.manga_title}
          </p>
        </div>

        <div className="w-12 h-px" style={{ background: 'var(--card-border-hover)' }} />

        {/* Dynamic section — changes based on endCardState */}
        {endCardState === 'loading' && (
          <div className="w-full space-y-3">
            <div className="h-3 rounded-full bg-zinc-800 animate-pulse w-20 mx-auto" />
            <div className="h-12 rounded-xl bg-zinc-800 animate-pulse w-full" />
          </div>
        )}

        {endCardState === 'translated' && nextChapter && (
          <div className="w-full space-y-3">
            <p className="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">
              Up Next
            </p>
            <p className="text-sm text-zinc-300 leading-snug">
              {nextChapterLabel}
              {nextChapter.chapter_title && (
                <span className="text-zinc-500"> — {nextChapter.chapter_title}</span>
              )}
            </p>
            {/* Use <a> so Next.js App Router soft-navigates without a full reload */}
            <a
              href={`/library/${nextChapter.id}`}
              className="btn-primary w-full py-3 text-sm font-semibold flex items-center justify-center gap-2"
              style={{ textDecoration: 'none' }}
            >
              Continue Reading
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 12h14M12 5l7 7-7 7" />
              </svg>
            </a>
          </div>
        )}

        {endCardState === 'untranslated' && (
          <div className="w-full space-y-3">
            <p className="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">
              Up Next
            </p>
            <p className="text-sm text-zinc-400 leading-snug">
              {nextChapterLabel}
              <span className="ml-1.5 text-zinc-600 text-xs">not yet translated</span>
            </p>
            <button
              onClick={onTranslate}
              disabled={translating}
              className="btn-primary w-full py-3 text-sm font-semibold flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-default"
            >
              {translating ? (
                <>
                  <svg className="animate-spin h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10"
                      stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Starting translation…
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
                  </svg>
                  Translate {nextChapterLabel}
                </>
              )}
            </button>
          </div>
        )}

        {endCardState === 'caught-up' && (
          <div className="space-y-2">
            <p className="text-3xl">🎌</p>
            <p className="text-sm font-semibold text-zinc-200">You&apos;re all caught up!</p>
            <p className="text-xs text-zinc-500 leading-relaxed">
              New chapters will appear here as they&apos;re translated.
            </p>
          </div>
        )}

        {/* Back to series — always shown */}
        <button
          onClick={onBack}
          className="text-xs text-zinc-600 hover:text-zinc-300 transition-colors flex items-center gap-1.5 mt-1"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
          Back to Series
        </button>
    </div>
  )
}

// ── Spinner ───────────────────────────────────────────────────────────────────

function ReaderSpinner() {
  return (
    <div className="flex items-center gap-3 text-zinc-500">
      <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
      <span>Loading chapter…</span>
    </div>
  )
}
