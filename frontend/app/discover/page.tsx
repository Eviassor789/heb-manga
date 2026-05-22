'use client'

import { useEffect, useRef, useState } from 'react'
import MangaCard from '@/components/MangaCard'
import SkeletonCard from '@/components/SkeletonCard'
import Spinner from '@/components/Spinner'

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

const MD_API = 'https://api.mangadex.org'
const ALL_RATINGS = ['safe', 'suggestive', 'erotica']  // fix: includes all ratings

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
  for (const [k, v] of Object.entries(extra)) url.searchParams.set(k, v)
  const res = await fetch(url.toString())
  if (!res.ok) throw new Error('MangaDex request failed')
  return (await res.json()).data ?? []
}

// ── Component ──────────────────────────────────────────────────────────────────

type Source = 'mangadex' | 'weebcentral'

export default function DiscoverPage() {
  const [source,        setSource]        = useState<Source>('mangadex')
  const [query,         setQuery]         = useState('')

  // MangaDex
  const [mdFeatured,    setMdFeatured]    = useState<MDManga[]>([])
  const [mdResults,     setMdResults]     = useState<MDManga[]>([])
  const [mdFeatLoading, setMdFeatLoading] = useState(true)
  const [mdSrchLoading, setMdSrchLoading] = useState(false)

  // WeebCentral
  const [wcFeatured,    setWcFeatured]    = useState<WCManga[]>([])
  const [wcResults,     setWcResults]     = useState<WCManga[]>([])
  const [wcFeatLoading, setWcFeatLoading] = useState(false)
  const [wcFeatLoaded,  setWcFeatLoaded]  = useState(false)
  const [wcSrchLoading, setWcSrchLoading] = useState(false)

  const [error,         setError]         = useState<string | null>(null)
  const [inLibrary,     setInLibrary]     = useState<Set<string>>(new Set())
  // Track whether user has actually submitted a search (Enter / button)
  // so typing alone never hides the featured grid.
  const [hasSearched,   setHasSearched]   = useState(false)

  const inputRef = useRef<HTMLInputElement>(null)

  // ── Library IDs for "In Library" badges ──────────────────────────────────

  useEffect(() => {
    fetch('/api/library')
      .then(r => r.json())
      .then(data => {
        const ids = new Set<string>(
          (data.chapters ?? []).map((c: { manga_id: string }) => c.manga_id)
        )
        setInLibrary(ids)
      })
      .catch(() => {})
  }, [])

  // ── MangaDex popular (load once on mount) ────────────────────────────────

  useEffect(() => {
    fetchMangaDex({ 'order[followedCount]': 'desc' })
      .then(setMdFeatured)
      .catch(() => setError('Could not load MangaDex. Check your connection.'))
      .finally(() => setMdFeatLoading(false))
  }, [])

  // ── WeebCentral featured (load when tab is first activated) ──────────────

  useEffect(() => {
    if (source !== 'weebcentral' || wcFeatLoaded) return
    setWcFeatLoading(true)
    fetch('/api/weebcentral/featured')
      .then(r => r.json())
      .then(d => setWcFeatured(d.results ?? []))
      .catch(() => {/* silently ignore — empty state shows instead */})
      .finally(() => { setWcFeatLoading(false); setWcFeatLoaded(true) })
  }, [source, wcFeatLoaded])

  // ── Search (triggered by Enter / button, NOT on every keystroke) ─────────

  const runSearch = async (q: string, src: Source) => {
    if (!q.trim()) return
    setHasSearched(true)

    if (src === 'mangadex') {
      setMdSrchLoading(true)
      try {
        setMdResults(await fetchMangaDex({ title: q, 'order[relevance]': 'desc' }))
      } catch {
        setMdResults([])
      } finally {
        setMdSrchLoading(false)
      }
    } else {
      setWcSrchLoading(true)
      try {
        const res = await fetch(`/api/search/weebcentral?q=${encodeURIComponent(q)}`)
        setWcResults((await res.json()).results ?? [])
      } catch {
        setWcResults([])
      } finally {
        setWcSrchLoading(false)
      }
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') runSearch(query, source)
  }

  const handleSearchBtn = () => runSearch(query, source)

  const clearSearch = () => {
    setQuery('')
    setMdResults([])
    setWcResults([])
    setHasSearched(false)  // go back to showing featured
    inputRef.current?.focus()
  }

  const handleSourceSwitch = (s: Source) => {
    setSource(s)
    setMdResults([])
    setWcResults([])
    setHasSearched(false)  // always show featured when switching tabs
  }

  // ── Derived state ─────────────────────────────────────────────────────────

  const isSearching = query.trim().length > 0
  const hasResults  = source === 'mangadex' ? mdResults.length > 0 : wcResults.length > 0

  const isLoading = source === 'mangadex'
    ? (hasSearched ? mdSrchLoading : mdFeatLoading)
    : (hasSearched ? wcSrchLoading : wcFeatLoading)

  // Show search results only after the user explicitly submits a search.
  // While typing, keep showing the featured grid unchanged.
  const displayList: (MDManga | WCManga)[] = source === 'mangadex'
    ? (hasSearched ? mdResults : mdFeatured)
    : (hasSearched ? wcResults : wcFeatured)

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen px-4 py-8 max-w-6xl mx-auto animate-fade-in">

      {/* ── Header ── */}
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-zinc-50 mb-1">Discover Manga</h1>
        <p className="text-zinc-500 text-sm">Find a manga and translate a chapter to Hebrew</p>
      </div>

      {/* ── Source toggle ── */}
      <div className="flex gap-1 bg-zinc-900/60 border border-[var(--card-border)] p-1 rounded-xl mb-5 w-fit">
        {(['mangadex', 'weebcentral'] as const).map(s => (
          <button
            key={s}
            onClick={() => handleSourceSwitch(s)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
              source === s
                ? 'bg-[var(--accent)] text-white shadow'
                : 'text-zinc-500 hover:text-zinc-200'
            }`}
          >
            {s === 'mangadex' ? '🟠 MangaDex' : '🟣 WeebCentral'}
          </button>
        ))}
      </div>

      {/* ── Search bar with button ── */}
      <div className="relative mb-8 flex gap-2">
        <div className="relative flex-1">
          {/* Spinner or icon inside the input */}
          <div className="absolute left-4 top-1/2 -translate-y-1/2 text-zinc-500 pointer-events-none">
            🔍
          </div>
          <input
            ref={inputRef}
            className="input pl-11 pr-10 text-base"
            placeholder={
              source === 'mangadex'
                ? 'Search MangaDex… (Enter to search)'
                : 'Search WeebCentral… (Enter to search)'
            }
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            autoFocus
          />
          {query && (
            <button
              onClick={clearSearch}
              className="absolute right-4 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors"
              aria-label="Clear search"
            >
              ✕
            </button>
          )}
        </div>

        {/* Search button */}
        <button
          onClick={handleSearchBtn}
          disabled={!query.trim() || isLoading}
          className="btn-primary px-5 flex items-center gap-2 shrink-0"
        >
          {isLoading && isSearching ? <Spinner size="sm" /> : null}
          Search
        </button>
      </div>

      {/* ── Section label ── */}
      <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-widest mb-4">
        {hasSearched && (displayList as WCManga[]).length > 0
          ? `Results for "${query}"`
          : hasSearched && (displayList as WCManga[]).length === 0 && !isLoading
          ? 'No results'
          : source === 'mangadex'
          ? 'Popular on MangaDex'
          : 'Hot Updates on WeebCentral'}
      </h2>

      {/* ── Error ── */}
      {error && source === 'mangadex' && (
        <div className="card p-6 text-center text-red-400 text-sm">{error}</div>
      )}

      {/* ── Loading skeletons ── */}
      {isLoading && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {Array.from({ length: 12 }).map((_, i) => <SkeletonCard key={i} />)}
        </div>
      )}

      {/* ── No results (only shown after an actual search) ── */}
      {!isLoading && hasSearched && displayList.length === 0 && (
        <div className="text-center py-16 text-zinc-500">
          No manga found for{' '}
          <span className="text-zinc-300">&ldquo;{query}&rdquo;</span>{' '}
          on {source === 'mangadex' ? 'MangaDex' : 'WeebCentral'}
        </div>
      )}

      {/* ── MangaDex grid ── */}
      {source === 'mangadex' && !isLoading && (displayList as MDManga[]).length > 0 && (
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

      {/* ── WeebCentral grid — links to our /weebcentral/[id] page ── */}
      {source === 'weebcentral' && !isLoading && (displayList as WCManga[]).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {(displayList as WCManga[]).map(m => (
            <MangaCard
              key={m.id}
              href={`/weebcentral/${m.id}`}
              title={m.title}
              coverUrl={m.cover || null}
              badge="WeebCentral"
              badgeColor="violet"
            />
          ))}
        </div>
      )}

    </main>
  )
}
