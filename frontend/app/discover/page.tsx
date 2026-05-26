'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import MangaCard from '@/components/MangaCard'
import SkeletonCard from '@/components/SkeletonCard'
import Spinner from '@/components/Spinner'
import { cacheGet, cacheSet } from '@/lib/cache'

// ── MangaDex API types ─────────────────────────────────────────────────────────

interface MDManga {
  id:            string
  attributes: {
    title:       Record<string, string>
    description: Record<string, string>
    status:      string
    tags:        { attributes: { name: Record<string, string> } }[]
  }
  relationships: {
    type:       string
    id:         string
    attributes?: { fileName?: string }
  }[]
}

interface WCManga {
  id:    string
  title: string
  cover: string
  url:   string
}

// ── Helpers ────────────────────────────────────────────────────────────────────

const MD_API      = 'https://api.mangadex.org'
const ALL_RATINGS = ['safe', 'suggestive']
const MD_EXCLUDED_TAGS = [
  'aafb99d1-7f60-43fa-b75f-fc9502ce29c7', // Harem
  '9438db5a-7e2a-4ac0-b39e-e0d95a34b8a8', // Reverse Harem
]

function getMangaTitle(m: MDManga): string {
  const t = m.attributes.title
  return t['en'] || t['ja-ro'] || Object.values(t)[0] || m.id
}

function getCoverUrl(m: MDManga): string | null {
  const rel = m.relationships.find(r => r.type === 'cover_art')
  if (!rel?.attributes?.fileName) return null
  return `https://uploads.mangadex.org/covers/${m.id}/${rel.attributes.fileName}.512.jpg`
}

async function fetchMangaDex(extra: Record<string, string>): Promise<MDManga[]> {
  const url = new URL(`${MD_API}/manga`)
  url.searchParams.set('limit', '24')
  url.searchParams.set('includes[]', 'cover_art')
  url.searchParams.set('availableTranslatedLanguage[]', 'en')
  ALL_RATINGS.forEach(r => url.searchParams.append('contentRating[]', r))
  MD_EXCLUDED_TAGS.forEach(id => url.searchParams.append('excludedTags[]', id))
  for (const [k, v] of Object.entries(extra)) url.searchParams.set(k, v)
  const res = await fetch(url.toString())
  if (!res.ok) throw new Error('MangaDex request failed')
  return (await res.json()).data ?? []
}

// ── Types ──────────────────────────────────────────────────────────────────────

type Source = 'mangadex' | 'weebcentral'

// ── Inner component (needs useSearchParams → must be inside Suspense) ──────────

function DiscoverContent() {
  const router       = useRouter()
  const searchParams = useSearchParams()

  // URL is the source of truth for what's actually been searched
  const urlQ   = searchParams.get('q')   ?? ''
  const urlSrc = (searchParams.get('src') ?? 'weebcentral') as Source

  // Local UI state — input value (can differ from urlQ while user is typing)
  const [inputValue, setInputValue] = useState(urlQ)

  // MangaDex state
  const [mdFeatured,    setMdFeatured]    = useState<MDManga[]>([])
  const [mdResults,     setMdResults]     = useState<MDManga[]>([])
  const [mdFeatLoading, setMdFeatLoading] = useState(true)
  const [mdSrchLoading, setMdSrchLoading] = useState(false)

  // WeebCentral state
  const [wcFeatured,    setWcFeatured]    = useState<WCManga[]>([])
  const [wcResults,     setWcResults]     = useState<WCManga[]>([])
  const [wcFeatLoading, setWcFeatLoading] = useState(false)
  const [wcFeatLoaded,  setWcFeatLoaded]  = useState(false)
  const [wcSrchLoading, setWcSrchLoading] = useState(false)

  const [error,     setError]     = useState<string | null>(null)
  const [inLibrary, setInLibrary] = useState<Set<string>>(new Set())

  const inputRef = useRef<HTMLInputElement>(null)

  // ── Library IDs for "In Library" badges ──────────────────────────────────

  useEffect(() => {
    fetch('/api/library')
      .then(r => r.json())
      .then(data => setInLibrary(new Set(
        (data.chapters ?? []).map((c: { manga_id: string }) => c.manga_id)
      )))
      .catch(() => {})
  }, [])

  // ── MangaDex popular (load once, cached 10 min) ───────────────────────────

  useEffect(() => {
    const cached = cacheGet<MDManga[]>('discover:md-featured', 10 * 60_000)
    if (cached) { setMdFeatured(cached); setMdFeatLoading(false); return }

    fetchMangaDex({ 'order[followedCount]': 'desc' })
      .then(data => { cacheSet('discover:md-featured', data); setMdFeatured(data) })
      .catch(() => setError('Could not load MangaDex. Check your connection.'))
      .finally(() => setMdFeatLoading(false))
  }, [])

  // ── WeebCentral featured (load when tab first activated, cached 10 min) ──

  useEffect(() => {
    if (urlSrc !== 'weebcentral' || wcFeatLoaded) return

    const cached = cacheGet<WCManga[]>('discover:wc-featured', 10 * 60_000)
    if (cached) { setWcFeatured(cached); setWcFeatLoading(false); setWcFeatLoaded(true); return }

    setWcFeatLoading(true)
    fetch('/api/weebcentral/featured')
      .then(r => r.json())
      .then(d => {
        const results = d.results ?? []
        cacheSet('discover:wc-featured', results)
        setWcFeatured(results)
      })
      .catch(() => {})
      .finally(() => { setWcFeatLoading(false); setWcFeatLoaded(true) })
  }, [urlSrc, wcFeatLoaded])

  // ── React to URL param changes (back/forward nav + initial load) ──────────
  // This is the single place that actually runs searches.

  const prevParamsRef = useRef('')

  useEffect(() => {
    const key = `${urlQ}|${urlSrc}`
    if (key === prevParamsRef.current) return   // nothing changed
    prevParamsRef.current = key

    // Sync input to URL value (restores the search term when pressing Back)
    setInputValue(urlQ)

    if (!urlQ.trim()) {
      // No query — clear results and show featured
      setMdResults([])
      setWcResults([])
      return
    }

    // Run the search for whatever the URL says (cached 5 min per query+source)
    const searchKey = `discover:search:${urlSrc}:${urlQ}`
    if (urlSrc === 'mangadex') {
      const cached = cacheGet<MDManga[]>(searchKey, 5 * 60_000)
      if (cached) { setMdResults(cached); return }
      setMdSrchLoading(true)
      fetchMangaDex({ title: urlQ, 'order[relevance]': 'desc' })
        .then(data => { cacheSet(searchKey, data); setMdResults(data) })
        .catch(() => setMdResults([]))
        .finally(() => setMdSrchLoading(false))
    } else {
      const cached = cacheGet<WCManga[]>(searchKey, 5 * 60_000)
      if (cached) { setWcResults(cached); return }
      setWcSrchLoading(true)
      fetch(`/api/search/weebcentral?q=${encodeURIComponent(urlQ)}`)
        .then(r => r.json())
        .then(d => {
          const results = d.results ?? []
          cacheSet(searchKey, results)
          setWcResults(results)
        })
        .catch(() => setWcResults([]))
        .finally(() => setWcSrchLoading(false))
    }
  }, [urlQ, urlSrc])

  // ── Push a new search to the URL (becomes a history entry → Back restores it) ──

  const commitSearch = useCallback((q: string, src: Source) => {
    if (!q.trim()) return
    const params = new URLSearchParams()
    params.set('q', q.trim())
    if (src !== 'weebcentral') params.set('src', src)
    router.push(`/discover?${params.toString()}`)
  }, [router])

  // ── Switch source tab (replace history — not a meaningful navigation step) ──

  const switchSource = useCallback((s: Source) => {
    const params = new URLSearchParams()
    if (urlQ) params.set('q', urlQ)
    if (s !== 'weebcentral') params.set('src', s)
    const qs = params.toString()
    router.replace(`/discover${qs ? `?${qs}` : ''}`)
  }, [router, urlQ])

  // ── Clear search ───────────────────────────────────────────────────────────

  const clearSearch = useCallback(() => {
    setInputValue('')
    const params = new URLSearchParams()
    if (urlSrc !== 'weebcentral') params.set('src', urlSrc)
    const qs = params.toString()
    router.push(`/discover${qs ? `?${qs}` : ''}`)
    inputRef.current?.focus()
  }, [router, urlSrc])

  // ── Derived ───────────────────────────────────────────────────────────────

  const hasSearched = !!urlQ.trim()

  const isLoading = urlSrc === 'mangadex'
    ? (hasSearched ? mdSrchLoading : mdFeatLoading)
    : (hasSearched ? wcSrchLoading : wcFeatLoading)

  const displayList: (MDManga | WCManga)[] = urlSrc === 'mangadex'
    ? (hasSearched ? mdResults : mdFeatured)
    : (hasSearched ? wcResults : wcFeatured)

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-6xl mx-auto animate-fade-in">

      {/* Header */}
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-zinc-50 mb-1">Discover Manga</h1>
        <p className="text-zinc-500 text-sm">Find a manga and translate a chapter to Hebrew</p>
      </div>

      {/* Source toggle */}
      <div className="flex gap-1 bg-zinc-900/60 border border-[var(--card-border)] p-1 rounded-xl mb-5 w-fit">
        {(['weebcentral', 'mangadex'] as const).map(s => (
          <button
            key={s}
            onClick={() => switchSource(s)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
              urlSrc === s
                ? 'bg-[var(--accent)] text-white shadow'
                : 'text-zinc-500 hover:text-zinc-200'
            }`}
          >
            {s === 'mangadex' ? '🟠 MangaDex' : '🟣 WeebCentral'}
          </button>
        ))}
      </div>

      {/* Search bar */}
      <div className="relative mb-8 flex gap-2">
        <div className="relative flex-1">
          <div className="absolute left-4 top-1/2 -translate-y-1/2 text-zinc-500 pointer-events-none">
            🔍
          </div>
          <input
            ref={inputRef}
            className="input pl-11 pr-10 text-base"
            placeholder={
              urlSrc === 'mangadex'
                ? 'Search MangaDex… (Enter to search)'
                : 'Search WeebCentral… (Enter to search)'
            }
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') commitSearch(inputValue, urlSrc) }}
            autoFocus
          />
          {inputValue && (
            <button
              onClick={clearSearch}
              className="absolute right-4 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors"
              aria-label="Clear search"
            >
              ✕
            </button>
          )}
        </div>

        <button
          onClick={() => commitSearch(inputValue, urlSrc)}
          disabled={!inputValue.trim() || isLoading}
          className="btn-primary px-5 flex items-center gap-2 shrink-0"
        >
          {isLoading && hasSearched ? <Spinner size="sm" /> : null}
          Search
        </button>
      </div>

      {/* Section label */}
      <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-widest mb-4">
        {hasSearched && displayList.length > 0 && !isLoading
          ? `Results for "${urlQ}"`
          : hasSearched && displayList.length === 0 && !isLoading
          ? 'No results'
          : urlSrc === 'mangadex'
          ? 'Popular on MangaDex'
          : 'Hot Updates on WeebCentral'}
      </h2>

      {/* Error */}
      {error && urlSrc === 'mangadex' && !hasSearched && (
        <div className="card p-6 text-center text-red-400 text-sm">{error}</div>
      )}

      {/* Loading skeletons */}
      {isLoading && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {Array.from({ length: 12 }).map((_, i) => <SkeletonCard key={i} />)}
        </div>
      )}

      {/* No results */}
      {!isLoading && hasSearched && displayList.length === 0 && (
        <div className="text-center py-16 text-zinc-500">
          No manga found for{' '}
          <span className="text-zinc-300">&ldquo;{urlQ}&rdquo;</span>{' '}
          on {urlSrc === 'mangadex' ? 'MangaDex' : 'WeebCentral'}
        </div>
      )}

      {/* MangaDex grid */}
      {urlSrc === 'mangadex' && !isLoading && (displayList as MDManga[]).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {(displayList as MDManga[]).map(m => (
            <MangaCard
              key={m.id}
              href={`/manga/${m.id}`}
              title={getMangaTitle(m)}
              coverUrl={getCoverUrl(m)}
              badge={inLibrary.has(m.id) ? '✓ In Library' : undefined}
              badgeColor="green"
            />
          ))}
        </div>
      )}

      {/* WeebCentral grid */}
      {urlSrc === 'weebcentral' && !isLoading && (displayList as WCManga[]).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {(displayList as WCManga[]).map(m => (
            <MangaCard
              key={m.id}
              href={`/weebcentral/${m.id}`}
              title={m.title}
              coverUrl={m.cover || null}
              badge={inLibrary.has(m.id) ? '✓ In Library' : undefined}
              badgeColor="green"
            />
          ))}
        </div>
      )}

    </main>
  )
}

// ── Page export — wraps in Suspense because useSearchParams requires it ────────

export default function DiscoverPage() {
  return (
    <Suspense
      fallback={
        <main className="min-h-screen flex items-center justify-center">
          <div className="flex items-center gap-3 text-zinc-500">
            <Spinner size="lg" />
            <span>Loading…</span>
          </div>
        </main>
      }
    >
      <DiscoverContent />
    </Suspense>
  )
}
