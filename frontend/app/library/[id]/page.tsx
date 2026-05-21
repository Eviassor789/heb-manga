'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'

// ── Types ──────────────────────────────────────────────────────────────────────

interface Chapter {
  id:            string
  manga_title:   string
  chapter_num:   string | null
  chapter_title: string | null
  cover_url:     string | null
  page_count:    number | null
  pdf_url:       string | null
  pages_prefix:  string | null
  pdf_size_kb:   number | null
  translated_at: string
}

// ── Page URL helper ───────────────────────────────────────────────────────────

function pageUrl(prefix: string, idx: number): string {
  // Pages are stored as 001.jpg, 002.jpg, … (matching typesetter NNN output naming)
  return `${prefix}/${String(idx).padStart(3, '0')}.jpg`
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function ReaderPage() {
  const { id } = useParams<{ id: string }>()
  const router  = useRouter()

  const [chapter,     setChapter]     = useState<Chapter | null>(null)
  const [loading,     setLoading]     = useState(true)
  const [error,       setError]       = useState<string | null>(null)
  const [currentPage, setCurrentPage] = useState(1)
  const [loadedPages, setLoadedPages] = useState<Set<number>>(new Set())
  const [headerVisible, setHeaderVisible] = useState(true)
  const lastScrollY = useRef(0)
  const pageRefs    = useRef<(HTMLDivElement | null)[]>([])
  const containerRef = useRef<HTMLDivElement>(null)

  // ── Fetch chapter metadata ─────────────────────────────────────────────────

  useEffect(() => {
    if (!id) return
    fetch(`/api/library/${id}`)
      .then(r => {
        if (!r.ok) throw new Error('Chapter not found')
        return r.json()
      })
      .then(data => {
        setChapter(data)
        setLoading(false)
      })
      .catch(err => {
        setError(err.message)
        setLoading(false)
      })
  }, [id])

  // ── Auto-hide header on scroll down ───────────────────────────────────────

  useEffect(() => {
    const onScroll = () => {
      const y = window.scrollY
      setHeaderVisible(y < lastScrollY.current || y < 80)
      lastScrollY.current = y
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  // ── Intersection observer → update current page indicator ─────────────────

  useEffect(() => {
    if (!chapter?.page_count) return
    const obs = new IntersectionObserver(
      entries => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const idx = Number((entry.target as HTMLElement).dataset.page)
            if (!isNaN(idx)) setCurrentPage(idx)
          }
        }
      },
      { threshold: 0.5 },
    )
    pageRefs.current.forEach(el => el && obs.observe(el))
    return () => obs.disconnect()
  }, [chapter?.page_count, loadedPages])

  // ── Keyboard navigation (← → keys) ───────────────────────────────────────

  const scrollToPage = useCallback((n: number) => {
    const el = pageRefs.current[n - 1]
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  useEffect(() => {
    const count = chapter?.page_count ?? 0
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        scrollToPage(Math.min(currentPage + 1, count))
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        scrollToPage(Math.max(currentPage - 1, 1))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [chapter?.page_count, currentPage, scrollToPage])

  // ── Render: loading / error states ────────────────────────────────────────

  if (loading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-500">
          <Spinner />
          <span>Loading chapter…</span>
        </div>
      </main>
    )
  }

  if (error || !chapter) {
    return (
      <main className="min-h-screen flex items-center justify-center flex-col gap-4">
        <p className="text-red-400">{error ?? 'Chapter not found'}</p>
        <button onClick={() => router.push('/library')} className="btn-ghost">
          ← Back to library
        </button>
      </main>
    )
  }

  const pageCount    = chapter.page_count ?? 0
  const pagesPrefix  = chapter.pages_prefix
  const chapterLabel = chapter.chapter_num
    ? `Chapter ${chapter.chapter_num}${chapter.chapter_title ? ` — ${chapter.chapter_title}` : ''}`
    : chapter.chapter_title ?? 'Chapter'

  // ── Render: reader ────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-zinc-950" ref={containerRef}>

      {/* ── Sticky header (auto-hides on scroll) ── */}
      <header
        className={`fixed top-0 left-0 right-0 z-50 transition-transform duration-300 ${
          headerVisible ? 'translate-y-0' : '-translate-y-full'
        }`}
      >
        <div className="bg-zinc-900/95 backdrop-blur border-b border-zinc-800 px-4 py-3">
          <div className="max-w-3xl mx-auto flex items-center gap-3">
            {/* Back */}
            <button
              onClick={() => router.push('/library')}
              className="shrink-0 text-zinc-500 hover:text-zinc-200 text-sm transition-colors flex items-center gap-1"
            >
              ← Library
            </button>

            {/* Title */}
            <div className="flex-1 min-w-0 text-center">
              <p className="text-sm font-semibold text-zinc-100 truncate">{chapter.manga_title}</p>
              <p className="text-xs text-zinc-500 truncate">{chapterLabel}</p>
            </div>

            {/* Page counter */}
            <div className="shrink-0 text-xs text-zinc-500 tabular-nums font-mono">
              {currentPage} / {pageCount}
            </div>
          </div>

          {/* Progress bar */}
          <div className="max-w-3xl mx-auto mt-2">
            <div className="h-0.5 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full transition-all duration-300"
                style={{ width: `${pageCount > 0 ? (currentPage / pageCount) * 100 : 0}%` }}
              />
            </div>
          </div>
        </div>
      </header>

      {/* ── Page images ── */}
      <div className="pt-16 pb-24">
        {pagesPrefix && pageCount > 0 ? (
          Array.from({ length: pageCount }, (_, i) => i + 1).map(n => (
            <div
              key={n}
              ref={el => { pageRefs.current[n - 1] = el }}
              data-page={n}
              className="max-w-3xl mx-auto mb-1"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={pageUrl(pagesPrefix, n)}
                alt={`Page ${n}`}
                className="w-full block"
                loading={n <= 3 ? 'eager' : 'lazy'}
                decoding="async"
                onLoad={() => setLoadedPages(prev => new Set([...prev, n]))}
                onError={e => {
                  // Show a placeholder on load error instead of broken image
                  const img = e.target as HTMLImageElement
                  img.style.display = 'none'
                  img.nextElementSibling?.classList.remove('hidden')
                }}
              />
              {/* Broken-image fallback */}
              <div className="hidden bg-zinc-900 border border-zinc-800 rounded-lg py-16 text-center text-zinc-600 text-sm">
                Page {n} unavailable
              </div>
            </div>
          ))
        ) : (
          <div className="max-w-3xl mx-auto pt-32 text-center space-y-4">
            <p className="text-zinc-600 text-sm">Page images not available.</p>
            {chapter.pdf_url && (
              <a href={chapter.pdf_url} download className="btn-primary inline-flex">
                ⬇ Download PDF instead
              </a>
            )}
          </div>
        )}
      </div>

      {/* ── Floating footer toolbar ── */}
      <div className="fixed bottom-0 left-0 right-0 z-50">
        <div className="max-w-3xl mx-auto px-4 pb-4">
          <div className="bg-zinc-900/95 backdrop-blur border border-zinc-700 rounded-2xl px-4 py-3 flex items-center gap-3 shadow-xl">

            {/* Prev page */}
            <button
              onClick={() => scrollToPage(Math.max(currentPage - 1, 1))}
              disabled={currentPage <= 1}
              className="shrink-0 w-9 h-9 rounded-xl border border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition-all disabled:opacity-30 disabled:cursor-default flex items-center justify-center"
            >
              ←
            </button>

            {/* Page number input */}
            <div className="flex-1 text-center text-sm text-zinc-400 tabular-nums select-none">
              {currentPage} <span className="text-zinc-600">/</span> {pageCount}
            </div>

            {/* Next page */}
            <button
              onClick={() => scrollToPage(Math.min(currentPage + 1, pageCount))}
              disabled={currentPage >= pageCount}
              className="shrink-0 w-9 h-9 rounded-xl border border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition-all disabled:opacity-30 disabled:cursor-default flex items-center justify-center"
            >
              →
            </button>

            <div className="w-px h-6 bg-zinc-700 mx-1" />

            {/* Download PDF */}
            {chapter.pdf_url && (
              <a
                href={chapter.pdf_url}
                download
                className="shrink-0 px-3 py-1.5 rounded-xl bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium transition-colors flex items-center gap-1.5 no-underline"
              >
                ⬇ PDF
                {chapter.pdf_size_kb && (
                  <span className="text-blue-200">
                    {Math.round(chapter.pdf_size_kb / 1024 * 10) / 10}MB
                  </span>
                )}
              </a>
            )}
          </div>
        </div>
      </div>

    </div>
  )
}

// ── Spinner ───────────────────────────────────────────────────────────────────

function Spinner() {
  return (
    <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}
