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
  url:         string
}

interface WCChapter {
  id:     string
  number: string
  title:  string
  url:    string
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

  // UI state
  const [sortDesc,     setSortDesc]     = useState(true)   // true = newest (highest ch#) first
  const [translating,  setTranslating]  = useState<string | null>(null)

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

  // ── Sorted chapter list ──────────────────────────────────────────────────

  const sorted = [...chapters].sort((a, b) => {
    const na = parseFloat(a.number || '0') || 0
    const nb = parseFloat(b.number || '0') || 0
    return sortDesc ? nb - na : na - nb
  })

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

      {/* Back */}
      <Link
        href="/discover"
        className="text-zinc-500 hover:text-[var(--accent)] text-sm transition-colors mb-6 inline-flex items-center gap-1"
      >
        ← Discover
      </Link>

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
            <p className="text-zinc-400 text-sm leading-relaxed line-clamp-4 mb-4">
              {series.description}
            </p>
          )}

          <div className="flex flex-wrap gap-3 text-sm">
            {!chapLoading && (
              <span className="text-zinc-400">
                <span className="text-zinc-200 font-bold">{chapters.length}</span>{' '}
                chapters available
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

      {/* ── Chapter list header ── */}
      <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
        <h2 className="text-lg font-bold text-zinc-100">Chapters</h2>

        {!chapLoading && chapters.length > 0 && (
          <button
            onClick={() => setSortDesc(d => !d)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium text-zinc-400 hover:text-zinc-200 transition-colors"
            style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}
            title={sortDesc ? 'Showing newest first — click for oldest first' : 'Showing oldest first — click for newest first'}
          >
            {sortDesc ? '↓ Newest' : '↑ Oldest'}
          </button>
        )}
      </div>

      {/* Loading skeleton */}
      {chapLoading && (
        <div className="card overflow-hidden">
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
        <div className="card divide-y divide-zinc-800/40 overflow-hidden">
          {sorted.length === 0 && (
            <div className="px-5 py-10 text-center text-zinc-500 text-sm">
              No chapters found. The series page structure may have changed.
            </div>
          )}

          {sorted.map(ch => {
            // WeebCentral "titles" are always "Chapter X" — just show the number.
            // Fall back to the raw title or ID only when number is missing.
            const label = ch.number ? `Ch. ${ch.number}` : ch.title || ch.id

            const isBusy = translating === ch.id

            return (
              <div
                key={ch.id}
                className="flex items-center gap-3 px-4 py-3 hover:bg-[var(--accent-subtle)] transition-colors"
              >
                <div
                  className="shrink-0 w-2 h-2 rounded-full"
                  style={{ background: 'var(--card-border-hover)' }}
                />

                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-zinc-400 truncate">{label}</p>
                </div>

                {/* Open chapter on WeebCentral */}
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

                {/* Translate to Hebrew */}
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
              </div>
            )
          })}
        </div>
      )}

    </main>
  )
}
