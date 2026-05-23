'use client'

import { useCallback, useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import Spinner from '@/components/Spinner'

// ── Types ──────────────────────────────────────────────────────────────────────

interface WCSeriesInfo {
  id:          string
  title:       string
  cover:       string
  description: string
  tags:        string[]
  url:         string
}

interface WCChapter {
  id:     string
  number: string
  title:  string
  url:    string
}

interface LibraryEntry {
  id:           string   // library UUID  → /library/{id}
  mangadex_id:  string   // "wc:ULID"
  chapter_num:  string
  chapter_title: string
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function WeebCentralMangaPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()

  // Phase 1: series metadata
  const [series,       setSeries]       = useState<WCSeriesInfo | null>(null)
  const [metaLoading,  setMetaLoading]  = useState(true)
  const [metaError,    setMetaError]    = useState<string | null>(null)

  // Phase 2: chapters
  const [chapters,     setChapters]     = useState<WCChapter[]>([])
  const [chapLoading,  setChapLoading]  = useState(true)

  // Phase 3: library (translated chapters)
  const [libMap,       setLibMap]       = useState<Map<string, LibraryEntry>>(new Map())

  // UI state
  const [sortDesc,     setSortDesc]     = useState(true)   // true = newest (highest ch#) first
  const [filter,       setFilter]       = useState<'all' | 'translated' | 'untranslated'>('all')
  const [translating,  setTranslating]  = useState<string | null>(null)
  const [currentBatch, setCurrentBatch] = useState(0)
  const BATCH_SIZE = 100

  // ── Phase 1: fetch series metadata ──────────────────────────────────────

  useEffect(() => {
    if (!id) return
    setMetaLoading(true)
    fetch(`/api/weebcentral/series/${id}`)
      .then(r => r.json())
      .then(d => setSeries(d as WCSeriesInfo))
      .catch(e => setMetaError(String(e)))
      .finally(() => setMetaLoading(false))
  }, [id])

  // ── Phase 2: fetch chapter list ──────────────────────────────────────────

  useEffect(() => {
    if (!id) return
    setChapLoading(true)
    fetch(`/api/weebcentral/series/${id}/chapters`)
      .then(r => r.json())
      .then(d => setChapters(d.chapters ?? []))
      .catch(() => setChapters([]))
      .finally(() => setChapLoading(false))
  }, [id])

  // ── Phase 3: fetch translated chapters from library ───────────────────────
  // manga_id for WeebCentral chapters is the series ULID (same as `id` param).

  useEffect(() => {
    if (!id) return
    fetch(`/api/library/manga/${id}`)
      .then(r => r.ok ? r.json() : { chapters: [] })
      .then(data => {
        const entries = (data.chapters ?? []) as LibraryEntry[]
        // Build lookup by chapter_num  (e.g. "378" → library entry)
        // Also index by wc chapter ULID extracted from mangadex_id ("wc:ULID")
        const map = new Map<string, LibraryEntry>()
        for (const entry of entries) {
          if (entry.chapter_num) map.set(entry.chapter_num, entry)
          const wcId = entry.mangadex_id?.startsWith('wc:')
            ? entry.mangadex_id.slice(3)
            : null
          if (wcId) map.set(wcId, entry)
        }
        setLibMap(map)
      })
      .catch(() => {/* library unreachable — just show no badges */})
  }, [id])

  // ── Translate a WeebCentral chapter ──────────────────────────────────────

  const handleTranslate = useCallback(async (ch: WCChapter) => {
    setTranslating(ch.id)
    try {
      const res  = await fetch('/api/jobs/from-url', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ url: ch.url, data_saver: false }),
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

  // ── Batch-aware chapter list ──────────────────────────────────────────────

  // Filter first
  const filtered = chapters.filter(ch => {
    const translated = !!(libMap.get(ch.number) ?? libMap.get(ch.id))
    if (filter === 'translated')   return  translated
    if (filter === 'untranslated') return !translated
    return true
  })

  // Always sort ascending — gives stable batches regardless of display direction
  const filteredAsc = [...filtered].sort((a, b) => {
    const na = parseFloat(a.number || '0') || 0
    const nb = parseFloat(b.number || '0') || 0
    return na - nb
  })

  // Group by chapter NUMBER range so that 48.5 falls in the same batch as 48
  function getChBatch(numStr: string): number {
    return Math.floor((parseFloat(numStr || '0') || 0) / BATCH_SIZE)
  }

  const allBatchNums = [...new Set(filteredAsc.map(ch => getChBatch(ch.number)))].sort((a, b) => a - b)
  const totalBatches = allBatchNums.length

  function batchLabel(batchIdx: number): string {
    const n = allBatchNums[batchIdx]
    return `${n * BATCH_SIZE + 1}–${(n + 1) * BATCH_SIZE}`
  }

  const activeBatchNum = allBatchNums[currentBatch] ?? 0
  const batchSlice = filteredAsc.filter(ch => getChBatch(ch.number) === activeBatchNum)
  const sorted = sortDesc ? [...batchSlice].reverse() : batchSlice

  // Default to LAST batch (newest) when filter / sort / data changes
  useEffect(() => {
    const batchNums = new Set(
      chapters
        .filter(ch => {
          const translated = !!(libMap.get(ch.number) ?? libMap.get(ch.id))
          if (filter === 'translated')   return  translated
          if (filter === 'untranslated') return !translated
          return true
        })
        .map(ch => getChBatch(ch.number))
    )
    setCurrentBatch(Math.max(0, batchNums.size - 1))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, sortDesc, chapters.length, libMap.size])

  const translatedCount = chapters.filter(ch =>
    !!(libMap.get(ch.number) ?? libMap.get(ch.id))
  ).length

  // ── Render: loading ────────────────────────────────────────────────────────

  if (metaLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-500">
          <Spinner size="lg" />
          <span>Loading from WeebCentral…</span>
        </div>
      </main>
    )
  }

  if (metaError || !series) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center gap-4">
        <p className="text-red-400">{metaError ?? 'Series not found on WeebCentral'}</p>
        <Link href="/discover" className="btn-ghost">← Back to Discover</Link>
      </main>
    )
  }

  // ── Render: series detail ──────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-5xl mx-auto animate-fade-in">

      {/* ── Series header ── */}
      <div className="flex gap-6 mb-10 flex-col sm:flex-row">
        {/* Cover */}
        {series.cover && (
          <div
            className="shrink-0 w-36 sm:w-44 rounded-xl overflow-hidden"
            style={{ border: '1px solid var(--card-border)' }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={series.cover}
              alt={series.title}
              className="w-full h-full object-cover aspect-[3/4]"
              loading="lazy"
            />
          </div>
        )}

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="badge-violet">WeebCentral</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold text-zinc-50 mb-3 leading-tight">
            {series.title}
          </h1>

          {series.description && (
            <p className="text-zinc-400 text-sm leading-relaxed line-clamp-4 mb-3">
              {series.description}
            </p>
          )}

          {/* Genre tags */}
          {series.tags && series.tags.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-4">
              {series.tags.map(tag => (
                <a
                  key={tag}
                  href={`https://weebcentral.com/search?included_tag=${encodeURIComponent(tag)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs font-medium px-2 py-0.5 rounded-md transition-colors hover:text-white"
                  style={{
                    background:  'rgba(10,5,20,0.7)',
                    border:      '1px solid rgba(139,92,246,0.3)',
                    color:       '#a78bfa',
                  }}
                >
                  {tag}
                </a>
              ))}
            </div>
          )}

          <div className="flex flex-wrap gap-3 text-sm">
            {!chapLoading && (
              <span className="text-zinc-400">
                <span className="text-zinc-200 font-bold">{chapters.length}</span>{' '}
                chapters available
              </span>
            )}
            {!chapLoading && translatedCount > 0 && (
              <span className="text-zinc-400">
                <span className="font-bold" style={{ color: 'var(--accent)' }}>{translatedCount}</span>{' '}
                in Hebrew
              </span>
            )}
            <a
              href={series.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-zinc-500 hover:text-[var(--accent)] transition-colors text-xs"
            >
              WeebCentral ↗
            </a>
          </div>
        </div>
      </div>

      {/* ── Chapter list header + controls ── */}
      <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">

        {/* Left: title + batch chips (capped at 60% so chips never crowd the filters) */}
        <div className="flex-1 min-w-0" style={{ maxWidth: '60%' }}>
          <h2 className="text-lg font-bold text-zinc-100 mb-2">Chapters</h2>
          {!chapLoading && totalBatches > 1 && (
            <div className="flex flex-wrap gap-1.5">
              {Array.from({ length: totalBatches }).map((_, i) => (
                <button
                  key={i}
                  onClick={() => setCurrentBatch(i)}
                  className="px-2.5 py-1 rounded-lg text-xs font-medium transition-all"
                  style={
                    i === currentBatch
                      ? { background: 'var(--accent)', color: '#fff' }
                      : { background: 'var(--card-bg)', border: '1px solid var(--card-border)', color: '#71717a' }
                  }
                >
                  {batchLabel(i)}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Right: filter + sort — always anchored to the right */}
        {!chapLoading && chapters.length > 0 && (
          <div className="flex items-center gap-2 flex-wrap shrink-0" style={{ margin: 'auto', marginBottom: '0px', marginRight: '0px' }}>
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

      {/* Loading skeleton */}
      {chapLoading && (
        <div className="rounded-2xl overflow-hidden" style={{ background: 'var(--card-bg)' }}>
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-4 px-4 py-3.5 border-b border-zinc-800/40 last:border-0 animate-pulse"
            >
              <div className="w-2 h-2 rounded-full bg-zinc-700 shrink-0" />
              <div className="flex-1 space-y-1.5">
                <div className="h-3 bg-zinc-800 rounded w-1/3" />
              </div>
              <div className="h-6 w-28 bg-zinc-800 rounded-lg shrink-0" />
              <div className="h-6 w-24 bg-zinc-800 rounded-lg shrink-0" />
            </div>
          ))}
        </div>
      )}

      {/* Chapter rows */}
      {!chapLoading && (
        <div className="rounded-2xl divide-y divide-zinc-800/40 overflow-hidden" style={{ background: 'var(--card-bg)' }}>
          {sorted.length === 0 && (
            <div className="px-5 py-10 text-center text-zinc-500 text-sm">
              {chapters.length === 0
                ? 'No chapters found. The series page structure may have changed.'
                : 'No chapters match this filter.'}
            </div>
          )}

          {sorted.map(ch => {
            // WeebCentral "titles" are always "Chapter X" — just show the number.
            // Fall back to the raw title or ID only when number is missing.
            const label = ch.number ? `Ch. ${ch.number}` : ch.title || ch.id

            const isBusy    = translating === ch.id
            // Match by chapter number or by the WC chapter ULID
            const libEntry  = libMap.get(ch.number) ?? libMap.get(ch.id)
            const translated = !!libEntry

            return (
              <div
                key={ch.id}
                className="flex items-center gap-3 px-4 py-3 hover:bg-[var(--accent-subtle)] transition-colors"
              >
                {/* Dot — green when translated, grey otherwise */}
                <div
                  className="shrink-0 w-2 h-2 rounded-full"
                  style={{ background: translated ? 'var(--accent)' : 'var(--card-border-hover)' }}
                />

                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-zinc-400 truncate">{label}</p>
                </div>

                {/* Primary action: Read Hebrew (translated) or Translate (not yet) */}
                {translated ? (
                  <Link
                    href={`/library/${libEntry!.id}`}
                    className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all"
                    style={{
                      background: 'rgba(34,197,94,0.12)',
                      border:     '1px solid rgba(34,197,94,0.3)',
                      color:      '#4ade80',
                    }}
                  >
                    ✓ Read Hebrew
                  </Link>
                ) : (
                  <button
                    onClick={() => handleTranslate(ch)}
                    disabled={isBusy || translating !== null}
                    className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all disabled:opacity-40 disabled:cursor-not-allowed"
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
                )}

                {/* Secondary: open on WeebCentral */}
                <a
                  href={ch.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="shrink-0 flex items-center px-2.5 py-1.5 rounded-lg text-xs transition-all text-zinc-500 hover:text-zinc-200"
                  style={{ border: '1px solid var(--card-border)' }}
                  title="Read on WeebCentral"
                >
                  ↗
                </a>
              </div>
            )
          })}
        </div>
      )}

    </main>
  )
}
