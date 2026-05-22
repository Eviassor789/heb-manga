'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import MangaCard from '@/components/MangaCard'
import SkeletonCard from '@/components/SkeletonCard'
import Spinner from '@/components/Spinner'

// ── Types ──────────────────────────────────────────────────────────────────────

interface LibraryChapter {
  id:            string
  mangadex_id:   string   // MangaDex chapter UUID
  manga_id:      string   // MangaDex manga UUID
  manga_title:   string
  chapter_num:   string | null
  chapter_title: string | null
  cover_url:     string | null
  page_count:    number | null
  translated_at: string
}

interface MangaSeries {
  manga_id:      string
  manga_title:   string
  cover_url:     string | null
  chapter_count: number
  latest_at:     string   // ISO timestamp of the most recent chapter
}

// ── Helpers ────────────────────────────────────────────────────────────────────

/**
 * MangaDex IDs are UUIDs:  xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
 * WeebCentral IDs are ULIDs: 26 uppercase alphanumeric chars (no dashes)
 * We use the presence of dashes to distinguish them.
 */
function seriesHref(manga_id: string): string {
  if (!manga_id) return '/discover'
  // ULID: 26 chars, no hyphens
  if (/^[0-9A-HJKMNP-TV-Z]{26}$/i.test(manga_id)) return `/weebcentral/${manga_id}`
  return `/manga/${manga_id}`
}

function groupBySeries(chapters: LibraryChapter[]): MangaSeries[] {
  const map = new Map<string, MangaSeries>()
  for (const ch of chapters) {
    const key = ch.manga_id || ch.manga_title
    if (!map.has(key)) {
      map.set(key, {
        manga_id:      ch.manga_id,
        manga_title:   ch.manga_title,
        cover_url:     ch.cover_url,
        chapter_count: 0,
        latest_at:     ch.translated_at,
      })
    }
    const s = map.get(key)!
    s.chapter_count++
    if (ch.translated_at > s.latest_at) s.latest_at = ch.translated_at
  }
  // Sort by most-recently-translated first
  return [...map.values()].sort((a, b) => b.latest_at.localeCompare(a.latest_at))
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function LibraryPage() {
  const [chapters,       setChapters]       = useState<LibraryChapter[]>([])
  const [loading,        setLoading]        = useState(true)
  const [libraryEnabled, setLibraryEnabled] = useState(true)
  const [search,         setSearch]         = useState('')

  useEffect(() => {
    fetch('/api/library')
      .then(r => r.json())
      .then(data => {
        setChapters(data.chapters ?? [])
        setLibraryEnabled(data.library_enabled ?? false)
      })
      .catch(() => {/* backend not reachable — show empty state */})
      .finally(() => setLoading(false))
  }, [])

  const series = useMemo(() => {
    const all = groupBySeries(chapters)
    if (!search.trim()) return all
    const q = search.toLowerCase()
    return all.filter(s => s.manga_title.toLowerCase().includes(q))
  }, [chapters, search])

  const totalChapters = chapters.length
  const totalSeries   = groupBySeries(chapters).length

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-6xl mx-auto">

      {/* ── Hero ── */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-zinc-50 mb-1">
          📚 Hebrew Manga Library
        </h1>
        {!loading && (
          <p className="text-zinc-500 text-sm">
            {totalSeries > 0
              ? `${totalSeries} series · ${totalChapters} chapters translated into Hebrew`
              : libraryEnabled
              ? 'No translations yet — be the first!'
              : 'Library not configured — see setup guide'}
          </p>
        )}
      </div>

      {/* ── Search + CTA ── */}
      <div className="flex gap-3 mb-8 flex-wrap">
        <input
          className="input flex-1 min-w-48 text-sm"
          placeholder="Filter by manga title…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <Link href="/discover" className="btn-primary flex items-center gap-2 whitespace-nowrap">
          <span>🔍</span> Find manga
        </Link>
      </div>

      {/* ── Loading grid ── */}
      {loading && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {Array.from({ length: 12 }).map((_, i) => <SkeletonCard key={i} />)}
        </div>
      )}

      {/* ── Not configured ── */}
      {!loading && !libraryEnabled && (
        <div className="card p-10 text-center max-w-lg mx-auto mt-12 space-y-4">
          <p className="text-4xl">🔧</p>
          <h2 className="text-lg font-semibold text-zinc-200">Library not set up</h2>
          <p className="text-zinc-400 text-sm leading-relaxed">
            The library is always active — translated chapters are saved locally.
            To host files on Cloudflare R2 (permanent public URLs), add your R2
            credentials to{' '}
            <code className="bg-zinc-800 px-1.5 py-0.5 rounded text-xs text-zinc-300">backend/.env</code>.
          </p>
          <Link href="/translate" className="btn-primary inline-flex mt-2">
            Translate a chapter
          </Link>
        </div>
      )}

      {/* ── Empty state (library enabled but no chapters yet) ── */}
      {!loading && libraryEnabled && series.length === 0 && !search && (
        <div className="card p-10 text-center max-w-lg mx-auto mt-12 space-y-4">
          <p className="text-5xl">📭</p>
          <h2 className="text-xl font-semibold text-zinc-200">The library is empty</h2>
          <p className="text-zinc-400 text-sm leading-relaxed">
            Translate your first chapter and it will appear here for everyone to read.
          </p>
          <Link href="/discover" className="btn-primary inline-flex gap-2">
            <span>🔍</span> Find a manga to translate
          </Link>
        </div>
      )}

      {/* ── No search results ── */}
      {!loading && series.length === 0 && search && (
        <div className="text-center py-16 text-zinc-500">
          No manga matching <span className="text-zinc-300">&ldquo;{search}&rdquo;</span>
        </div>
      )}

      {/* ── Manga grid ── */}
      {!loading && series.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {series.map(s => (
            <MangaCard
              key={s.manga_id || s.manga_title}
              href={seriesHref(s.manga_id)}
              title={s.manga_title}
              coverUrl={s.cover_url}
              subtitle={`${s.chapter_count} chapter${s.chapter_count !== 1 ? 's' : ''} in Hebrew`}
            />
          ))}

          {/* "+" card to discover more */}
          <Link
            href="/discover"
            className="group flex flex-col items-center justify-center aspect-[3/4] rounded-xl border-2 border-dashed border-[var(--card-border)] hover:border-[var(--accent)] hover:bg-[var(--accent-subtle)] transition-all duration-200 text-zinc-600 hover:text-[var(--accent)]"
          >
            <span className="text-3xl mb-2 group-hover:scale-110 transition-transform">＋</span>
            <span className="text-xs font-medium">Add manga</span>
          </Link>
        </div>
      )}

    </main>
  )
}
