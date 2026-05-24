'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import MangaCover from '@/components/MangaCover'
import Spinner from '@/components/Spinner'

// ── Shared normalised types ────────────────────────────────────────────────────

interface SeriesInfo {
  title:       string
  coverUrl:    string | null
  description: string
  author?:     string
  tags:        string[]
  externalUrl: string
}

interface NormalizedChapter {
  key:          string         // stable React key + translating-state key
  number:       string | null
  label:        string
  externalUrl:  string | null
  pages?:       number
  publishAt?:   string
  translateUrl: string
  lookupKeys:   string[]       // all keys to check in the lib map
}

interface LibraryEntry {
  id: string
}

// ── MangaDex raw types ────────────────────────────────────────────────────────

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

// ── WeebCentral raw types ─────────────────────────────────────────────────────

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

// ── MangaDex helpers ──────────────────────────────────────────────────────────

const MD_API = 'https://api.mangadex.org'

function getMDTitle(m: MDManga): string {
  const t = m.attributes.title
  return t['en'] || t['ja-ro'] || Object.values(t)[0] || m.id
}

function getMDCoverUrl(m: MDManga): string | null {
  const rel = m.relationships.find(r => r.type === 'cover_art')
  if (!rel?.attributes?.fileName) return null
  return `https://uploads.mangadex.org/covers/${m.id}/${rel.attributes.fileName}.512.jpg`
}

function getMDAuthor(m: MDManga): string {
  return m.relationships.find(r => r.type === 'author')?.attributes?.name ?? ''
}

function getMDDesc(m: MDManga): string {
  const d = m.attributes.description
  return d['en'] || Object.values(d)[0] || ''
}

function getMDTags(m: MDManga): string[] {
  return m.attributes.tags
    .map(t => t.attributes.name['en'] || Object.values(t.attributes.name)[0])
    .filter(Boolean)
    .slice(0, 5)
}

async function fetchAllMDChapters(mangaId: string): Promise<MDChapter[]> {
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

    if (batch.length === 0 || all.length >= total) break
  }

  return all
}

// ── Props ──────────────────────────────────────────────────────────────────────

interface SeriesDetailPageProps {
  id:     string
  source: 'mangadex' | 'weebcentral'
}

// ── Constants ─────────────────────────────────────────────────────────────────

const BATCH_SIZE = 100

// ── Component ──────────────────────────────────────────────────────────────────

export default function SeriesDetailPage({ id, source }: SeriesDetailPageProps) {
  const router = useRouter()

  // ── Phase-1: series metadata ──────────────────────────────────────────────
  const [series,      setSeries]      = useState<SeriesInfo | null>(null)
  const [metaLoading, setMetaLoading] = useState(true)
  const [metaError,   setMetaError]   = useState<string | null>(null)

  // ── Phase-2: chapter list ─────────────────────────────────────────────────
  const [chapters,    setChapters]    = useState<NormalizedChapter[]>([])
  const [chapLoading, setChapLoading] = useState(true)

  // ── Phase-3: library hits ─────────────────────────────────────────────────
  const [libMap,      setLibMap]      = useState<Map<string, LibraryEntry>>(new Map())

  // ── UI state ──────────────────────────────────────────────────────────────
  const [filter,       setFilter]       = useState<'all' | 'translated' | 'untranslated'>('all')
  const [sortDesc,     setSortDesc]     = useState(true)
  const [currentBatch, setCurrentBatch] = useState(0)
  const [translating,  setTranslating]  = useState<string | null>(null)

  // ── Phase 1: fetch series metadata ────────────────────────────────────────

  useEffect(() => {
    if (!id) return
    setMetaLoading(true)
    setSeries(null)
    setMetaError(null)

    const p = source === 'mangadex'
      ? fetch(`${MD_API}/manga/${id}?includes[]=cover_art&includes[]=author`)
          .then(r => r.json())
          .then((j): SeriesInfo => {
            const m = j.data as MDManga
            return {
              title:       getMDTitle(m),
              coverUrl:    getMDCoverUrl(m),
              description: getMDDesc(m),
              author:      getMDAuthor(m),
              tags:        getMDTags(m),
              externalUrl: `https://mangadex.org/title/${m.id}`,
            }
          })
      : fetch(`/api/weebcentral/series/${id}`)
          .then(r => r.json())
          .then((d: WCSeriesInfo): SeriesInfo => ({
            title:       d.title,
            coverUrl:    d.cover || null,
            description: d.description,
            tags:        d.tags ?? [],
            externalUrl: d.url,
          }))

    p.then(s => setSeries(s))
      .catch(e => setMetaError(String(e)))
      .finally(() => setMetaLoading(false))
  }, [id, source])

  // ── Phase 2: fetch & normalise chapters ──────────────────────────────────

  useEffect(() => {
    if (!id) return
    setChapLoading(true)
    setChapters([])

    const p = source === 'mangadex'
      ? fetchAllMDChapters(id).then((raw): NormalizedChapter[] => {
          // Deduplicate by chapter number — keep first occurrence
          const seen = new Set<string>()
          return raw
            .filter(ch => {
              const key = ch.attributes.chapter ?? ch.id
              if (seen.has(key)) return false
              seen.add(key); return true
            })
            .map(ch => ({
              key:          ch.id,
              number:       ch.attributes.chapter,
              label:        ch.attributes.chapter
                ? (ch.attributes.title
                    ? `Ch. ${ch.attributes.chapter} — ${ch.attributes.title}`
                    : `Ch. ${ch.attributes.chapter}`)
                : 'Oneshot',
              externalUrl:  `https://mangadex.org/chapter/${ch.id}`,
              pages:        ch.attributes.pages,
              publishAt:    ch.attributes.publishAt,
              translateUrl: `https://mangadex.org/chapter/${ch.id}`,
              lookupKeys:   [ch.id],
            }))
        })
      : fetch(`/api/weebcentral/series/${id}/chapters`)
          .then(r => r.json())
          .then((d): NormalizedChapter[] =>
            ((d.chapters ?? []) as WCChapter[]).map(ch => ({
              key:          ch.id,
              number:       ch.number || null,
              label:        ch.number ? `Ch. ${ch.number}` : ch.title || ch.id,
              externalUrl:  ch.url,
              translateUrl: ch.url,
              lookupKeys:   [ch.number, ch.id].filter(Boolean),
            }))
          )

    p.then(chs => setChapters(chs))
      .catch(() => setChapters([]))
      .finally(() => setChapLoading(false))
  }, [id, source])

  // ── Phase 3: fetch library hits ───────────────────────────────────────────

  useEffect(() => {
    if (!id) return
    fetch(`/api/library/manga/${id}`)
      .then(r => r.ok ? r.json() : { chapters: [] })
      .then(data => {
        const entries: { id: string; mangadex_id: string; chapter_num: string }[] =
          data.chapters ?? []
        const map = new Map<string, LibraryEntry>()
        for (const entry of entries) {
          // MangaDex: keyed by chapter UUID stored in mangadex_id
          if (entry.mangadex_id && !entry.mangadex_id.startsWith('wc:')) {
            map.set(entry.mangadex_id, { id: entry.id })
          }
          // WeebCentral: keyed by chapter_num string
          if (entry.chapter_num) map.set(entry.chapter_num, { id: entry.id })
          // WeebCentral: keyed by bare ULID stripped from "wc:ULID"
          if (entry.mangadex_id?.startsWith('wc:')) {
            map.set(entry.mangadex_id.slice(3), { id: entry.id })
          }
        }
        setLibMap(map)
      })
      .catch(() => {/* library unreachable — show no badges */})
  }, [id])

  // ── Translation trigger ───────────────────────────────────────────────────

  const handleTranslate = useCallback(async (ch: NormalizedChapter) => {
    setTranslating(ch.key)
    try {
      const res  = await fetch('/api/jobs/from-url', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ url: ch.translateUrl, data_saver: false }),
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

  // ── Batch + filter helpers ────────────────────────────────────────────────

  function isTranslatedCh(ch: NormalizedChapter): boolean {
    return ch.lookupKeys.some(k => libMap.has(k))
  }

  const filtered = chapters.filter(ch => {
    if (filter === 'translated')   return  isTranslatedCh(ch)
    if (filter === 'untranslated') return !isTranslatedCh(ch)
    return true
  })

  // Always sort ascending — batches are stable regardless of display direction
  const filteredAsc = [...filtered].sort((a, b) => {
    const na = parseFloat(a.number ?? '0') || 0
    const nb = parseFloat(b.number ?? '0') || 0
    return na - nb
  })

  function getChBatch(numStr: string | null): number {
    return Math.floor((parseFloat(numStr ?? '0') || 0) / BATCH_SIZE)
  }

  const allBatchNums = [...new Set(filteredAsc.map(ch => getChBatch(ch.number)))].sort((a, b) => a - b)
  const totalBatches = allBatchNums.length
  const activeBatchNum = allBatchNums[currentBatch] ?? 0
  const batchSlice = filteredAsc.filter(ch => getChBatch(ch.number) === activeBatchNum)

  // Label shows the ACTUAL first and last chapter numbers in the batch
  function batchLabel(batchIdx: number): string {
    const n     = allBatchNums[batchIdx]
    const items = filteredAsc.filter(ch => getChBatch(ch.number) === n)
    if (items.length === 0) return `${n * BATCH_SIZE}–${(n + 1) * BATCH_SIZE - 1}`
    const clean = (s: string | null) => {
      if (!s) return '?'
      const num = parseFloat(s)
      return isNaN(num) ? s : Number.isInteger(num) ? String(num) : s
    }
    return `${clean(items[0].number)}–${clean(items[items.length - 1].number)}`
  }

  const sorted = sortDesc ? [...batchSlice].reverse() : batchSlice

  // Reset to last batch whenever filter / sort / data changes
  useEffect(() => {
    const batchNums = new Set(
      chapters
        .filter(ch => {
          if (filter === 'translated')   return  isTranslatedCh(ch)
          if (filter === 'untranslated') return !isTranslatedCh(ch)
          return true
        })
        .map(ch => getChBatch(ch.number))
    )
    setCurrentBatch(Math.max(0, batchNums.size - 1))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, sortDesc, chapters.length, libMap.size])

  const translatedCount = chapters.filter(isTranslatedCh).length

  // ── Loading / error states ────────────────────────────────────────────────

  if (metaLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-500">
          <Spinner size="lg" />
          <span>{source === 'weebcentral' ? 'Loading from WeebCentral…' : 'Loading manga…'}</span>
        </div>
      </main>
    )
  }

  if (metaError || !series) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center gap-4">
        <p className="text-red-400">{metaError ?? 'Series not found'}</p>
        <Link href="/discover" className="btn-ghost">← Back to Discover</Link>
      </main>
    )
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-5xl mx-auto animate-fade-in">

      {/* ── Series header ── */}
      <div className="flex gap-6 mb-10 flex-col sm:flex-row">

        {/* Cover */}
        <div
          className="shrink-0 w-36 sm:w-44 rounded-xl overflow-hidden"
          style={{ border: '1px solid var(--card-border)' }}
        >
          <MangaCover src={series.coverUrl} alt={series.title} />
        </div>

        {/* Meta */}
        <div className="flex-1 min-w-0">

          <h1 className="text-2xl sm:text-3xl font-bold text-zinc-50 mb-1 leading-tight">
            {series.title}
          </h1>

          {series.author && (
            <p className="text-zinc-400 text-sm mb-3">{series.author}</p>
          )}

          {/* Genre tags */}
          {series.tags.length > 0 && (
            source === 'weebcentral'
              ? (
                /* WC: plain dot-separated text → in-app search */
                <p className="text-xs text-zinc-500 mb-4 leading-relaxed">
                  {series.tags.map((tag, i) => (
                    <span key={tag}>
                      {i > 0 && <span className="text-zinc-700"> · </span>}
                      <Link
                        href={`/discover?q=${encodeURIComponent(tag)}`}
                        className="hover:text-[#e4b7e3] transition-colors"
                      >
                        {tag}
                      </Link>
                    </span>
                  ))}
                </p>
              )
              : (
                /* MD: subtle badge chips */
                <div className="flex flex-wrap gap-1.5 mb-4">
                  {series.tags.map(tag => (
                    <span
                      key={tag}
                      className="px-2 py-0.5 rounded-lg text-xs"
                      style={{
                        background: 'var(--card-bg)',
                        border:     '1px solid var(--card-border)',
                        color:      '#e4b7e3',
                      }}
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )
          )}

          {series.description && (
            <p className="text-zinc-400 text-sm leading-relaxed line-clamp-4 mb-4">
              {series.description}
            </p>
          )}

          {/* Stats row */}
          <div className="flex flex-wrap gap-4 text-sm">
            {!chapLoading && translatedCount > 0 && (
              <div className="flex items-center gap-1.5">
                <span className="font-bold" style={{ color: '#e4b7e3' }}>{translatedCount}</span>
                <span className="text-zinc-500">chapters in Hebrew</span>
              </div>
            )}
            {!chapLoading && (
              <div className="flex items-center gap-1.5">
                <span className="font-bold" style={{ color: '#e4b7e3' }}>{chapters.length}</span>
                <span className="text-zinc-500">total chapters</span>
              </div>
            )}
            <a
              href={series.externalUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-zinc-500 transition-colors text-sm"
              style={{ color: '#e4b7e3'}}
            >
              {source === 'weebcentral' ? 'WeebCentral' : 'MangaDex'} ↗
            </a>
          </div>
        </div>
      </div>

      {/* ── Chapter list header + controls ── */}
      <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">

        {/* Left: "Chapters" title + batch chips (max 60% so they don't crowd the filters) */}
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

        {/* Right: filter + sort — anchored to the right */}
        {!chapLoading && (
          <div
            className="flex items-center gap-2 flex-wrap shrink-0"
            style={{ margin: 'auto', marginBottom: '0px', marginRight: '0px' }}
          >
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

      {/* Loading skeleton while chapters are fetched */}
      {chapLoading && (
        <div className="rounded-2xl overflow-hidden" style={{ background: 'var(--card-bg)' }}>
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
        <div
          className="rounded-2xl divide-y divide-zinc-800/40 overflow-hidden"
          style={{ background: 'var(--card-bg)' }}
        >
          {sorted.length === 0 && (
            <div className="px-5 py-10 text-center text-zinc-500 text-sm">
              {chapters.length === 0
                ? (source === 'mangadex'
                    ? 'No English chapters found on MangaDex for this title.'
                    : 'No chapters found. The series page structure may have changed.')
                : 'No chapters match this filter.'}
            </div>
          )}

          {sorted.map(ch => {
            const libEntry     = ch.lookupKeys.map(k => libMap.get(k)).find(Boolean)
            const isTranslated = !!libEntry
            const isBusy       = translating === ch.key

            return (
              <div
                key={ch.key}
                className="group flex items-center gap-3 px-4 py-3 hover:bg-[var(--accent-subtle)] transition-colors"
              >
                {/* Status dot */}
                <div
                  className="shrink-0 w-2 h-2 rounded-full"
                  style={{ background: isTranslated ? '#22c55e' : 'var(--card-border-hover)' }}
                />

                {/* Chapter info */}
                <div className="flex-1 min-w-0">
                  <p className={`text-sm font-medium truncate transition-colors group-hover:text-[#e4b7e3] ${isTranslated ? 'text-zinc-200' : 'text-zinc-400'}`}>
                    {ch.label}
                  </p>
                  {(ch.pages || ch.publishAt) && (
                    <p className="text-xs text-zinc-600 mt-0.5">
                      {ch.pages && ch.pages > 0 ? `${ch.pages}p` : ''}
                      {ch.pages && ch.pages > 0 && ch.publishAt ? ' · ' : ''}
                      {ch.publishAt
                        ? new Date(ch.publishAt).toLocaleDateString(undefined, {
                            year: 'numeric', month: 'short', day: 'numeric',
                          })
                        : ''}
                    </p>
                  )}
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
                  /* ── Not translated: translate + external read ── */
                  <div className="shrink-0 flex items-center gap-1.5">
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

                    {ch.externalUrl && (
                      <a
                        href={ch.externalUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center px-2.5 py-1.5 rounded-lg text-xs transition-all text-zinc-500 hover:text-zinc-200"
                        style={{ border: '1px solid var(--card-border)' }}
                        title={`Read on ${source === 'weebcentral' ? 'WeebCentral' : 'MangaDex'}`}
                      >
                        ↗
                      </a>
                    )}
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
