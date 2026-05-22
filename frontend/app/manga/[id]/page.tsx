'use client'

import { useCallback, useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import MangaCover from '@/components/MangaCover'
import Spinner from '@/components/Spinner'

// ── MangaDex types ─────────────────────────────────────────────────────────────

interface MDManga {
  id: string
  attributes: {
    title:       Record<string, string>
    description: Record<string, string>
    status:      string
    year:        number | null
    tags:        { attributes: { name: Record<string, string> } }[]
  }
  relationships: {
    type:        string
    id:          string
    attributes?: { fileName?: string; name?: string }
  }[]
}

interface MDChapter {
  id: string
  attributes: {
    chapter:            string | null
    title:              string | null
    translatedLanguage: string
    pages:              number
    publishAt:          string
  }
}

interface LibChapter {
  id:            string   // Supabase UUID → /library/[id] reader URL
  mangadex_id:   string   // MangaDex chapter UUID
  chapter_num:   string | null
  chapter_title: string | null
}

// ── Helpers ────────────────────────────────────────────────────────────────────

const MD_API = 'https://api.mangadex.org'

function getTitle(m: MDManga): string {
  const t = m.attributes.title
  return t['en'] || t['ja-ro'] || Object.values(t)[0] || m.id
}

function getCoverUrl(m: MDManga): string | null {
  const rel = m.relationships.find(r => r.type === 'cover_art')
  if (!rel?.attributes?.fileName) return null
  return `https://uploads.mangadex.org/covers/${m.id}/${rel.attributes.fileName}.512.jpg`
}

function getAuthor(m: MDManga): string {
  return m.relationships.find(r => r.type === 'author')?.attributes?.name ?? ''
}

function getDesc(m: MDManga): string {
  const d = m.attributes.description
  return d['en'] || Object.values(d)[0] || ''
}

function getTags(m: MDManga): string[] {
  return m.attributes.tags
    .map(t => t.attributes.name['en'] || Object.values(t.attributes.name)[0])
    .filter(Boolean)
    .slice(0, 5)
}

function chapterLabel(ch: MDChapter): string {
  const num = ch.attributes.chapter ? `Ch. ${ch.attributes.chapter}` : 'Oneshot'
  return ch.attributes.title ? `${num} — ${ch.attributes.title}` : num
}

/**
 * Fetch ALL English chapters for a manga, paginating 100 at a time.
 *
 * MangaDex returns at most 500 per request; using smaller batches of 100
 * avoids hitting limits and handles large manga (500+ chapters) correctly.
 * This also fixes missing chapters on manga like Frieren that may have many
 * scanlation group entries.
 */
async function fetchAllChapters(mangaId: string): Promise<MDChapter[]> {
  const all: MDChapter[] = []
  let offset = 0
  const limit = 100

  for (;;) {
    const url = new URL(`${MD_API}/manga/${mangaId}/feed`)
    url.searchParams.set('translatedLanguage[]', 'en')
    url.searchParams.set('order[chapter]', 'asc')
    url.searchParams.set('limit', String(limit))
    url.searchParams.set('offset', String(offset))

    const res = await fetch(url.toString())
    if (!res.ok) break

    const json  = await res.json()
    const batch: MDChapter[] = json.data ?? []
    const total: number      = json.total ?? 0

    all.push(...batch)
    offset += batch.length

    // Stop when we've fetched everything or got an empty page
    if (batch.length === 0 || all.length >= total) break
  }

  return all
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function MangaPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()

  // ── Phase-1 state: manga metadata (fast) ─────────────────────────────────
  const [manga,       setManga]       = useState<MDManga | null>(null)
  const [mangaLoading,setMangaLoading]= useState(true)
  const [mangaError,  setMangaError]  = useState<string | null>(null)

  // ── Phase-2 state: chapter list + library (can be slow) ──────────────────
  const [chapters,    setChapters]    = useState<MDChapter[]>([])
  const [libChapters, setLibChapters] = useState<Map<string, LibChapter>>(new Map())
  const [chapLoading, setChapLoading] = useState(true)

  // ── UI state ──────────────────────────────────────────────────────────────
  const [translating, setTranslating] = useState<string | null>(null)
  const [filter,      setFilter]      = useState<'all' | 'translated' | 'untranslated'>('all')
  const [sortDesc,    setSortDesc]    = useState(true)   // true = newest (highest ch#) first

  // ── Phase 1: fetch manga metadata immediately ─────────────────────────────

  useEffect(() => {
    if (!id) return
    setMangaLoading(true)
    fetch(`${MD_API}/manga/${id}?includes[]=cover_art&includes[]=author`)
      .then(r => r.json())
      .then(j => setManga(j.data as MDManga))
      .catch(e => setMangaError(String(e)))
      .finally(() => setMangaLoading(false))
  }, [id])

  // ── Phase 2: fetch chapters + library data (parallel, no dependency on manga) ─

  useEffect(() => {
    if (!id) return
    setChapLoading(true)

    Promise.all([
      fetchAllChapters(id),
      fetch(`/api/library/manga/${id}`)
        .then(r => r.json())
        .then(j => (j.chapters ?? []) as LibChapter[])
        .catch(() => [] as LibChapter[]),
    ]).then(([chapData, libData]) => {
      // Deduplicate by chapter number — keep earliest uploaded
      const seen = new Set<string>()
      const deduped = chapData.filter(ch => {
        const key = ch.attributes.chapter ?? ch.id
        if (seen.has(key)) return false
        seen.add(key); return true
      })
      setChapters(deduped)

      const map = new Map<string, LibChapter>()
      for (const lc of libData) map.set(lc.mangadex_id, lc)
      setLibChapters(map)
    }).finally(() => setChapLoading(false))
  }, [id])

  // ── Translate a chapter ───────────────────────────────────────────────────

  const handleTranslate = useCallback(async (ch: MDChapter) => {
    setTranslating(ch.id)
    try {
      const res  = await fetch('/api/jobs/from-url', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ url: `https://mangadex.org/chapter/${ch.id}`, data_saver: false }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to start')
      if (data.cached && data.library_id) router.push(`/library/${data.library_id}`)
      else router.push(`/jobs/${data.job_id}`)
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : 'Something went wrong')
      setTranslating(null)
    }
  }, [router])

  // ── Filtered + sorted chapter list ───────────────────────────────────────

  const filtered = chapters.filter(ch => {
    if (filter === 'translated')   return  libChapters.has(ch.id)
    if (filter === 'untranslated') return !libChapters.has(ch.id)
    return true
  })

  const sorted = [...filtered].sort((a, b) => {
    const na = parseFloat(a.attributes.chapter ?? '0') || 0
    const nb = parseFloat(b.attributes.chapter ?? '0') || 0
    return sortDesc ? nb - na : na - nb
  })

  // ── Render: loading phase 1 ───────────────────────────────────────────────

  if (mangaLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-500">
          <Spinner size="lg" />
          <span>Loading manga…</span>
        </div>
      </main>
    )
  }

  if (mangaError || !manga) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center gap-4">
        <p className="text-red-400">{mangaError ?? 'Manga not found'}</p>
        <Link href="/discover" className="btn-ghost">← Back to Discover</Link>
      </main>
    )
  }

  const title           = getTitle(manga)
  const cover           = getCoverUrl(manga)
  const author          = getAuthor(manga)
  const description     = getDesc(manga)
  const tags            = getTags(manga)
  const translatedCount = chapters.filter(ch => libChapters.has(ch.id)).length

  // ── Render: manga detail + lazy chapter list ──────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-5xl mx-auto animate-fade-in">

      {/* Back */}
      <Link
        href="/discover"
        className="text-zinc-500 hover:text-[var(--accent)] text-sm transition-colors mb-6 inline-flex items-center gap-1"
      >
        ← Discover
      </Link>

      {/* ── Manga header — shown immediately ── */}
      <div className="flex gap-6 mb-10 flex-col sm:flex-row">
        <div
          className="shrink-0 w-36 sm:w-44 rounded-xl overflow-hidden"
          style={{ border: '1px solid var(--card-border)' }}
        >
          <MangaCover src={cover} alt={title} />
        </div>

        <div className="flex-1 min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold text-zinc-50 mb-1 leading-tight">{title}</h1>

          {author && <p className="text-zinc-400 text-sm mb-3">{author}</p>}

          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-4">
              {tags.map(tag => (
                <span
                  key={tag}
                  className="px-2 py-0.5 rounded-lg text-xs text-zinc-400"
                  style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}

          {description && (
            <p className="text-zinc-400 text-sm leading-relaxed line-clamp-4 mb-4">{description}</p>
          )}

          {/* Stats — update once chapters arrive */}
          <div className="flex flex-wrap gap-4 text-sm">
            {!chapLoading && (
              <div className="flex items-center gap-1.5">
                <span className="text-[var(--accent)] font-bold">{translatedCount}</span>
                <span className="text-zinc-500">chapters in Hebrew</span>
              </div>
            )}
            {!chapLoading && (
              <div className="flex items-center gap-1.5">
                <span className="text-zinc-300 font-bold">{chapters.length}</span>
                <span className="text-zinc-500">total chapters</span>
              </div>
            )}
            <a
              href={`https://mangadex.org/title/${manga.id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-zinc-500 hover:text-[var(--accent)] transition-colors text-xs"
            >
              MangaDex ↗
            </a>
          </div>
        </div>
      </div>

      {/* ── Chapter list — loads separately ── */}
      <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
        <h2 className="text-lg font-bold text-zinc-100">Chapters</h2>

        {!chapLoading && (
          <div className="flex items-center gap-2 flex-wrap">
            {/* Filter toggle */}
            <div
              className="flex items-center gap-1 p-1 rounded-xl text-xs"
              style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}
            >
              {(['all', 'translated', 'untranslated'] as const).map(f => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`px-3 py-1.5 rounded-lg font-medium transition-all ${
                    filter === f ? 'text-white' : 'text-zinc-500 hover:text-zinc-300'
                  }`}
                  style={filter === f ? { background: 'var(--accent)' } : undefined}
                >
                  {f === 'translated' ? '✓ Hebrew' : f === 'untranslated' ? '○ Untranslated' : 'All'}
                </button>
              ))}
            </div>

            {/* Sort toggle */}
            <button
              onClick={() => setSortDesc(d => !d)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium text-zinc-400 hover:text-zinc-200 transition-colors"
              style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}
              title={sortDesc ? 'Showing newest first — click for oldest first' : 'Showing oldest first — click for newest first'}
            >
              {sortDesc ? '↓ Newest' : '↑ Oldest'}
            </button>
          </div>
        )}
      </div>

      {/* Loading skeleton while chapters are fetched */}
      {chapLoading && (
        <div className="card overflow-hidden">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-4 px-4 py-3.5 border-b border-zinc-800/40 last:border-0 animate-pulse"
            >
              <div className="w-2 h-2 rounded-full bg-zinc-700 shrink-0" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 bg-zinc-800 rounded w-1/2" />
                <div className="h-2.5 bg-zinc-800 rounded w-1/4" />
              </div>
              <div className="h-6 w-24 bg-zinc-800 rounded-lg shrink-0" />
            </div>
          ))}
        </div>
      )}

      {/* Chapter rows */}
      {!chapLoading && (
        <div className="card divide-y divide-zinc-800/40 overflow-hidden">
          {sorted.length === 0 && (
            <div className="px-5 py-10 text-center text-zinc-500 text-sm">
              {chapters.length === 0
                ? 'No English chapters found on MangaDex for this title.'
                : 'No chapters match this filter.'}
            </div>
          )}

          {sorted.map(ch => {
            const libEntry     = libChapters.get(ch.id)
            const isTranslated = !!libEntry
            const isBusy       = translating === ch.id
            const label        = chapterLabel(ch)

            return (
              <div
                key={ch.id}
                className="flex items-center gap-3 px-4 py-3 hover:bg-[var(--accent-subtle)] transition-colors"
              >
                {/* Status dot */}
                <div
                  className="shrink-0 w-2 h-2 rounded-full"
                  style={{ background: isTranslated ? '#22c55e' : 'var(--card-border-hover)' }}
                />

                {/* Chapter info */}
                <div className="flex-1 min-w-0">
                  <p className={`text-sm font-medium truncate ${isTranslated ? 'text-zinc-200' : 'text-zinc-400'}`}>
                    {label}
                  </p>
                  <p className="text-xs text-zinc-600 mt-0.5">
                    {ch.attributes.pages > 0 ? `${ch.attributes.pages}p` : ''}
                    {ch.attributes.pages > 0 && ch.attributes.publishAt ? ' · ' : ''}
                    {ch.attributes.publishAt
                      ? new Date(ch.attributes.publishAt).toLocaleDateString(undefined, {
                          year: 'numeric', month: 'short', day: 'numeric',
                        })
                      : ''}
                  </p>
                </div>

                {/* Action buttons */}
                {isTranslated && libEntry ? (
                  /* ── Translated: read in our reader ── */
                  <Link
                    href={`/library/${libEntry.id}`}
                    className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors"
                    style={{
                      background: 'rgba(34,197,94,0.12)',
                      border:     '1px solid rgba(34,197,94,0.3)',
                      color:      '#4ade80',
                    }}
                  >
                    ✓ Read Hebrew
                  </Link>
                ) : (
                  /* ── Not translated: read source OR start translation ── */
                  <div className="shrink-0 flex items-center gap-1.5">
                    {/* Open MangaDex reader in new tab */}
                    <a
                      href={`https://mangadex.org/chapter/${ch.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center px-2.5 py-1.5 rounded-lg text-xs transition-all text-zinc-500 hover:text-zinc-200"
                      style={{ border: '1px solid var(--card-border)' }}
                      title="Read on MangaDex"
                    >
                      ↗
                    </a>

                    {/* Translate to Hebrew */}
                    <button
                      onClick={() => handleTranslate(ch)}
                      disabled={isBusy || translating !== null}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                      style={{
                        background: 'var(--accent-subtle)',
                        border:     '1px solid var(--card-border-hover)',
                        color:      '#c4b5fd',
                      }}
                      title="Translate this chapter to Hebrew"
                    >
                      {isBusy
                        ? <><Spinner size="sm" /> Starting…</>
                        : <>Translate →</>}
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

    </main>
  )
}
