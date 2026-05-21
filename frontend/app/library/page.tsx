'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'

// ── Types ──────────────────────────────────────────────────────────────────────

interface Chapter {
  id:            string
  mangadex_id:   string
  manga_id:      string
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

interface MangaGroup {
  manga_id:    string
  manga_title: string
  cover_url:   string | null
  chapters:    Chapter[]
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function groupByManga(chapters: Chapter[]): MangaGroup[] {
  const map = new Map<string, MangaGroup>()
  for (const ch of chapters) {
    const key = ch.manga_id || ch.manga_title
    if (!map.has(key)) {
      map.set(key, {
        manga_id:    ch.manga_id,
        manga_title: ch.manga_title,
        cover_url:   ch.cover_url,
        chapters:    [],
      })
    }
    map.get(key)!.chapters.push(ch)
  }
  // Sort chapters within each series by chapter_num
  for (const group of map.values()) {
    group.chapters.sort((a, b) => {
      const na = parseFloat(a.chapter_num ?? '0')
      const nb = parseFloat(b.chapter_num ?? '0')
      return na - nb
    })
  }
  return [...map.values()]
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function LibraryPage() {
  const [chapters,         setChapters]         = useState<Chapter[]>([])
  const [loading,          setLoading]          = useState(true)
  const [error,            setError]            = useState<string | null>(null)
  const [libraryEnabled,   setLibraryEnabled]   = useState(true)
  const [search,           setSearch]           = useState('')

  useEffect(() => {
    fetch('/api/library')
      .then(r => r.json())
      .then(data => {
        setChapters(data.chapters ?? [])
        setLibraryEnabled(data.library_enabled ?? false)
        setLoading(false)
      })
      .catch(() => {
        setError('Could not load library. Is the backend running?')
        setLoading(false)
      })
  }, [])

  const filtered = search.trim()
    ? chapters.filter(c =>
        c.manga_title.toLowerCase().includes(search.toLowerCase()) ||
        (c.chapter_title ?? '').toLowerCase().includes(search.toLowerCase())
      )
    : chapters

  const groups = groupByManga(filtered)

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-10">

      {/* Header */}
      <div className="max-w-5xl mx-auto mb-8">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div>
            <Link href="/" className="text-zinc-500 hover:text-zinc-300 text-sm transition-colors">
              ← Translate new chapter
            </Link>
            <h1 className="text-3xl font-bold text-zinc-50 mt-3 flex items-center gap-3">
              <span>📚</span> Hebrew Manga Library
            </h1>
            <p className="text-zinc-400 text-sm mt-1">
              {chapters.length} chapter{chapters.length !== 1 ? 's' : ''} translated · shared across all users
            </p>
          </div>

          {/* Search */}
          <input
            className="input w-64 text-sm"
            placeholder="Search manga…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* States */}
      {loading && (
        <div className="max-w-5xl mx-auto flex items-center justify-center py-24">
          <div className="flex items-center gap-3 text-zinc-500">
            <Spinner />
            <span>Loading library…</span>
          </div>
        </div>
      )}

      {!loading && error && (
        <div className="max-w-5xl mx-auto py-12 text-center">
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {!loading && !error && !libraryEnabled && (
        <div className="max-w-2xl mx-auto mt-12">
          <div className="card p-8 text-center space-y-4">
            <p className="text-4xl">🔧</p>
            <h2 className="text-lg font-semibold text-zinc-200">Library not configured</h2>
            <p className="text-zinc-400 text-sm leading-relaxed">
              To enable the shared library, add your Supabase and Cloudflare R2 credentials
              to <code className="text-zinc-300 bg-zinc-800 px-1.5 py-0.5 rounded text-xs">backend/.env</code>.
              See <code className="text-zinc-300 bg-zinc-800 px-1.5 py-0.5 rounded text-xs">backend/core/library.py</code> for
              the required variable names.
            </p>
            <Link href="/" className="btn-primary inline-flex mt-2">
              Translate a chapter
            </Link>
          </div>
        </div>
      )}

      {!loading && !error && libraryEnabled && groups.length === 0 && (
        <div className="max-w-2xl mx-auto mt-12">
          <div className="card p-8 text-center space-y-4">
            <p className="text-4xl">📭</p>
            <h2 className="text-lg font-semibold text-zinc-200">
              {search ? 'No results' : 'Library is empty'}
            </h2>
            <p className="text-zinc-400 text-sm">
              {search
                ? 'Try a different search term.'
                : 'Translate your first chapter and it will appear here for everyone.'}
            </p>
            {!search && (
              <Link href="/" className="btn-primary inline-flex mt-2">
                Translate a chapter
              </Link>
            )}
          </div>
        </div>
      )}

      {/* Manga grid */}
      {!loading && groups.length > 0 && (
        <div className="max-w-5xl mx-auto space-y-10">
          {groups.map(group => (
            <MangaSection key={group.manga_id || group.manga_title} group={group} />
          ))}
        </div>
      )}
    </main>
  )
}

// ── MangaSection ──────────────────────────────────────────────────────────────

function MangaSection({ group }: { group: MangaGroup }) {
  const [expanded, setExpanded] = useState(true)

  return (
    <section className="card p-0 overflow-hidden">
      {/* Series header */}
      <button
        className="w-full flex items-center gap-4 p-5 hover:bg-zinc-800/50 transition-colors text-left"
        onClick={() => setExpanded(v => !v)}
      >
        {/* Cover */}
        <div className="shrink-0 w-16 h-20 rounded-lg overflow-hidden bg-zinc-800 border border-zinc-700">
          {group.cover_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={group.cover_url}
              alt={group.manga_title}
              className="w-full h-full object-cover"
              loading="lazy"
              onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-2xl text-zinc-600">
              📖
            </div>
          )}
        </div>

        {/* Title + count */}
        <div className="flex-1 min-w-0">
          <h2 className="text-lg font-bold text-zinc-100 truncate">{group.manga_title}</h2>
          <p className="text-sm text-zinc-500 mt-0.5">
            {group.chapters.length} chapter{group.chapters.length !== 1 ? 's' : ''} · Hebrew translation
          </p>
        </div>

        <span className={`shrink-0 text-zinc-600 transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}>
          ▾
        </span>
      </button>

      {/* Chapter list */}
      {expanded && (
        <div className="border-t border-zinc-800">
          {group.chapters.map((ch, idx) => (
            <Link
              key={ch.id}
              href={`/library/${ch.id}`}
              className={`flex items-center gap-4 px-5 py-3.5 hover:bg-zinc-800/50 transition-colors group ${
                idx < group.chapters.length - 1 ? 'border-b border-zinc-800/60' : ''
              }`}
            >
              {/* Chapter badge */}
              <div className="shrink-0 w-12 h-12 rounded-lg bg-blue-950/60 border border-blue-800/60 flex items-center justify-center">
                <span className="text-blue-300 font-bold text-sm">
                  {ch.chapter_num ? `${ch.chapter_num}` : '?'}
                </span>
              </div>

              {/* Chapter info */}
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-zinc-200 truncate group-hover:text-white transition-colors">
                  {ch.chapter_title
                    ? `Chapter ${ch.chapter_num} — ${ch.chapter_title}`
                    : `Chapter ${ch.chapter_num ?? '?'}`}
                </p>
                <p className="text-xs text-zinc-600 mt-0.5">
                  {ch.page_count ? `${ch.page_count} pages` : ''}
                  {ch.page_count && ch.pdf_size_kb ? ' · ' : ''}
                  {ch.pdf_size_kb ? `${Math.round(ch.pdf_size_kb / 1024 * 10) / 10} MB` : ''}
                  {(ch.page_count || ch.pdf_size_kb) ? ' · ' : ''}
                  {fmtDate(ch.translated_at)}
                </p>
              </div>

              {/* Read button */}
              <div className="shrink-0 flex items-center gap-2 text-sm text-zinc-500 group-hover:text-blue-400 transition-colors">
                <span>Read</span>
                <span>→</span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </section>
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
