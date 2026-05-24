'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import MangaCard from '@/components/MangaCard'
import SkeletonCard from '@/components/SkeletonCard'

// ── Types ──────────────────────────────────────────────────────────────────────

interface LibraryChapter {
  id:            string
  mangadex_id:   string
  manga_id:      string
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
  latest_at:     string
}

interface MDManga {
  id: string
  attributes: {
    title:       Record<string, string>
    description: Record<string, string>
    tags:        { attributes: { name: Record<string, string> } }[]
  }
  relationships: { type: string; id: string; attributes?: { fileName?: string } }[]
}

interface WCManga {
  id:    string
  title: string
  cover: string
  url:   string
}

interface ReadingProgress {
  manga_id:    string
  manga_title: string
  cover_url:   string | null
  chapter_id:  string
  chapter_num: string | null
  last_read:   string
}

interface HeroSlide {
  id:           string
  title:        string
  coverUrl:     string | null
  description:  string
  href:         string
  badge?:       string
  badgeColor?:  'green' | 'violet' | 'orange'
  genres?:      string[]
  chapterCount?: number  // chapters translated to Hebrew (library manga)
}

// ── Helpers ────────────────────────────────────────────────────────────────────

const MD_API      = 'https://api.mangadex.org'
const ALL_RATINGS = ['safe', 'suggestive', 'erotica']

function seriesHref(manga_id: string): string {
  if (!manga_id) return '/discover'
  if (/^[0-9A-HJKMNP-TV-Z]{26}$/i.test(manga_id)) return `/weebcentral/${manga_id}`
  return `/manga/${manga_id}`
}

function getSource(manga_id: string): 'weebcentral' | 'mangadex' {
  return /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(manga_id) ? 'weebcentral' : 'mangadex'
}

function getMDTitle(m: MDManga): string {
  const t = m.attributes.title
  return t['en'] || t['ja-ro'] || Object.values(t)[0] || m.id
}

function getMDCover(m: MDManga): string | null {
  const rel = m.relationships.find(r => r.type === 'cover_art')
  if (!rel?.attributes?.fileName) return null
  return `https://uploads.mangadex.org/covers/${m.id}/${rel.attributes.fileName}.512.jpg`
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
  return [...map.values()].sort((a, b) => b.chapter_count - a.chapter_count)
}

// ── HeroCarousel ───────────────────────────────────────────────────────────────

function HeroCarousel({ slides, loading }: { slides: HeroSlide[]; loading: boolean }) {
  const [current, setCurrent] = useState(0)
  const [paused,  setPaused]  = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (paused || slides.length <= 1) return
    intervalRef.current = setInterval(() => setCurrent(c => (c + 1) % slides.length), 6000)
    return () => clearInterval(intervalRef.current!)
  }, [paused, slides.length])

  useEffect(() => { setCurrent(0) }, [slides.length])

  if (loading) {
    return (
      <div
        className="w-full animate-pulse"
        style={{ height: 'clamp(320px, 52vh, 520px)', background: 'rgba(139,92,246,0.05)' }}
      />
    )
  }

  if (slides.length === 0) {
    return (
      <div
        className="w-full flex flex-col items-center justify-center gap-4 text-center px-6"
        style={{ height: 'clamp(320px, 52vh, 520px)', background: 'radial-gradient(ellipse 80% 60% at 50% 50%, rgba(232,121,168,0.08) 0%, transparent 70%)' }}
      >
        <p className="text-5xl">🈺</p>
        <h1 className="text-3xl font-bold" style={{ color: 'var(--sakura)' }}>HeManga</h1>
        <p className="text-zinc-500 text-sm max-w-xs">Manga translated to Hebrew. Start by discovering and translating a chapter.</p>
        <Link href="/discover" className="btn-primary mt-2 px-8 py-3">Discover Manga →</Link>
      </div>
    )
  }

  const slide = slides[current]

  return (
    <div
      className="relative w-full overflow-hidden"
      style={{ height: 'clamp(320px, 52vh, 520px)' }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      {/* Blurred, darkened cover as background */}
      {slide.coverUrl ? (
        <img
          key={slide.id + '-bg'}
          src={slide.coverUrl}
          alt=""
          aria-hidden
          className="absolute inset-0 w-full h-full object-cover"
          style={{
            filter:     'blur(32px) brightness(0.32) saturate(1.5)',
            transform:  'scale(1.12)',
            transition: 'opacity 0.6s ease',
          }}
        />
      ) : (
        <div
          className="absolute inset-0"
          style={{ background: 'radial-gradient(ellipse 80% 60% at 30% 50%, rgba(139,92,246,0.12) 0%, transparent 70%)' }}
        />
      )}

      {/* Gradient overlays — left fade + bottom fade */}
      <div className="absolute inset-0 bg-gradient-to-r from-[#09090f] via-[#09090f]/70 to-[#09090f]/20" />
      <div className="absolute inset-0 bg-gradient-to-t from-[#09090f] via-[#09090f]/10 to-transparent" />

      {/* Content */}
      <div className="relative h-full max-w-7xl mx-auto px-6 sm:px-10 flex items-center">

        {/* Left text column — constrained so it doesn't overlap the cover */}
        <div className="min-w-0 max-w-lg xl:max-w-xl" key={slide.id}>
          <p className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-3">
            ✦ HeManga Featured
          </p>

          {/* Fixed-height badge + title + genres + chapter count — buttons never jump */}
          <div style={{ minHeight: '11rem' }}>
            {slide.badge && (
              <span className={`inline-block mb-3 ${
                slide.badgeColor === 'green'  ? 'badge-green'  :
                slide.badgeColor === 'orange' ? 'badge-orange' : 'badge-violet'
              }`}>
                {slide.badge}
              </span>
            )}

            <h2
              className="text-3xl sm:text-4xl lg:text-5xl font-bold leading-tight mb-3 line-clamp-2 animate-fade-in"
              style={{ color: 'var(--sakura)' }}
            >
              {slide.title}
            </h2>

            {/* Genres — plain dot-separated text, pink */}
            <p className="text-xs mb-2" style={{ minHeight: '1rem', color: '#e4b7e3' }}>
              {slide.genres && slide.genres.length > 0 ? slide.genres.slice(0, 5).join(' · ') : ''}
            </p>

            {/* Chapter count — only the number is pink */}
            <p className="text-zinc-500 text-xs mt-1" style={{ minHeight: '1.25rem' }}>
              {slide.chapterCount != null
                ? <><span style={{ color: '#e4b7e3' }}>{slide.chapterCount}</span>{` chapter${slide.chapterCount !== 1 ? 's' : ''} in Hebrew`}</>
                : slide.description || ''}
            </p>
          </div>

          <div className="flex items-center gap-3 flex-wrap mt-4">
            <Link href={slide.href} className="btn-primary px-6 py-2.5 text-sm">
              Read Now →
            </Link>
            <Link href={slide.href} className="btn-ghost px-5 py-2.5 text-sm">
              Details
            </Link>
          </div>
        </div>

      </div>

      {/* Cover — absolutely pinned to the right edge, larger */}
      {slide.coverUrl && (
        <div
          className="hidden md:block absolute"
          style={{
            right:     '20%',
            top:       '50%',
            transform: 'translateY(-50%)',
            width:     'clamp(190px, 20vw, 350px)',
            zIndex:    1,
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            key={slide.id + '-cover'}
            src={slide.coverUrl}
            alt={slide.title}
            className="w-full rounded-xl object-cover animate-fade-in"
            style={{
              aspectRatio: '3/4',
              boxShadow:   '0 24px 72px rgba(0,0,0,0.85), 0 0 0 1px var(--card-border-hover)',
            }}
          />
        </div>
      )}

      {/* Prev / Next arrows */}
      {slides.length > 1 && (
        <>
          <button
            aria-label="Previous"
            className="absolute left-3 top-1/2 -translate-y-1/2 w-9 h-9 rounded-full flex items-center justify-center text-zinc-300 hover:text-white transition-all text-lg"
            style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)' }}
            onClick={() => setCurrent(c => (c - 1 + slides.length) % slides.length)}
          >
            ‹
          </button>
          <button
            aria-label="Next"
            className="absolute right-3 top-1/2 -translate-y-1/2 w-9 h-9 rounded-full flex items-center justify-center text-zinc-300 hover:text-white transition-all text-lg"
            style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)' }}
            onClick={() => setCurrent(c => (c + 1) % slides.length)}
          >
            ›
          </button>
        </>
      )}

      {/* Dots */}
      {slides.length > 1 && (
        <div className="absolute bottom-4 left-0 right-0 flex justify-center gap-1.5">
          {slides.map((_, i) => (
            <button
              key={i}
              aria-label={`Slide ${i + 1}`}
              onClick={() => setCurrent(i)}
              className="rounded-full transition-all duration-300"
              style={{
                width:      i === current ? '22px' : '6px',
                height:     '6px',
                background: i === current ? 'var(--accent)' : 'rgba(255,255,255,0.22)',
              }}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ── RowSection — Netflix-style horizontal scroll row ───────────────────────────

function RowSection({
  title, href = '', hrefLabel = 'See all →',
  loading = false, empty = false, children,
}: {
  title:      string
  href?:      string
  hrefLabel?: string
  loading?:   boolean
  empty?:     boolean
  children?:  React.ReactNode
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const scroll = (dir: number) =>
    scrollRef.current?.scrollBy({ left: dir * 450, behavior: 'smooth' })

  if (!loading && empty) return null

  return (
    <section className="mb-10">
      {/* Row header */}
      <div className="flex items-center justify-between mb-4 px-4 sm:px-8 xl:px-32">
        <h2 className="font-bold" style={{ fontSize: '23px', color: '#e4b7e3' }}>{title}</h2>
        {href && hrefLabel && (
          <Link
            href={href}
            className="text-xs text-zinc-500 hover:text-[var(--accent)] transition-colors"
          >
            {hrefLabel}
          </Link>
        )}
      </div>

      {/* Scrollable container with full-height Netflix-style arrows */}
      <div className="relative group/row">

        {/* Left arrow */}
        <button
          aria-label="Scroll left"
          onClick={() => scroll(-1)}
          className="absolute left-0 top-0 bottom-0 z-10 w-16 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-opacity"
          style={{ background: 'linear-gradient(to right, rgba(9,9,15,0.88) 0%, transparent 100%)' }}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-white">
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>

        <div
          ref={scrollRef}
          className="hide-scrollbar flex gap-4 overflow-x-auto px-4 sm:px-8 xl:px-32 pb-2"
          style={{ scrollbarWidth: 'none' }}
        >
          {loading
            ? Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="w-36 sm:w-44 flex-shrink-0">
                  <SkeletonCard />
                </div>
              ))
            : children
          }
        </div>

        {/* Right arrow */}
        <button
          aria-label="Scroll right"
          onClick={() => scroll(1)}
          className="absolute right-0 top-0 bottom-0 z-10 w-16 flex items-center justify-center opacity-0 group-hover/row:opacity-100 transition-opacity"
          style={{ background: 'linear-gradient(to left, rgba(9,9,15,0.88) 0%, transparent 100%)' }}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-white">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>

      </div>
    </section>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function HomePage() {
  // ── State ───────────────────────────────────────────────────────────────────
  const [libraryChapters, setLibraryChapters] = useState<LibraryChapter[]>([])
  const [libLoading,       setLibLoading]       = useState(true)

  const [wcFeatured, setWcFeatured] = useState<WCManga[]>([])
  const [wcLoading,  setWcLoading]  = useState(true)

  const [mdPopular, setMdPopular] = useState<MDManga[]>([])
  const [mdLoading, setMdLoading] = useState(true)

  const [continueReading, setContinueReading] = useState<ReadingProgress[]>([])

  // genres keyed by manga_id / WC ULID — fetched lazily for hero slides
  const [heroGenres, setHeroGenres] = useState<Map<string, string[]>>(new Map())

  // ── Fetch all data in parallel ───────────────────────────────────────────────

  useEffect(() => {
    // Continue reading — client-side localStorage only
    try {
      const raw = localStorage.getItem('hemanga-continue-reading') ?? '[]'
      setContinueReading(JSON.parse(raw))
    } catch {}

    // Library
    fetch('/api/library')
      .then(r => r.json())
      .then(d => setLibraryChapters(d.chapters ?? []))
      .catch(() => {})
      .finally(() => setLibLoading(false))

    // WeebCentral featured
    fetch('/api/weebcentral/featured')
      .then(r => r.json())
      .then(d => setWcFeatured(d.results ?? []))
      .catch(() => {})
      .finally(() => setWcLoading(false))

    // MangaDex popular
    const mdUrl = new URL(`${MD_API}/manga`)
    mdUrl.searchParams.set('limit', '24')
    mdUrl.searchParams.set('includes[]', 'cover_art')
    mdUrl.searchParams.set('availableTranslatedLanguage[]', 'en')
    mdUrl.searchParams.set('order[followedCount]', 'desc')
    ALL_RATINGS.forEach(r => mdUrl.searchParams.append('contentRating[]', r))
    fetch(mdUrl.toString())
      .then(r => r.json())
      .then(d => setMdPopular(d.data ?? []))
      .catch(() => {})
      .finally(() => setMdLoading(false))
  }, [])

  // ── Derived ──────────────────────────────────────────────────────────────────

  const librarySeries = useMemo(() => groupBySeries(libraryChapters), [libraryChapters])

  const recentChapters = useMemo(() =>
    [...libraryChapters]
      .sort((a, b) => b.translated_at.localeCompare(a.translated_at))
      .slice(0, 20)
  , [libraryChapters])

  // Hero: library manga first (most chapters = most effort) → pad with MangaDex popular
  const heroSlides = useMemo((): HeroSlide[] => {
    // Library slides — sorted by chapter count descending
    const libSlides: HeroSlide[] = librarySeries.slice(0, 10).map(s => ({
      id:           s.manga_id,
      title:        s.manga_title,
      coverUrl:     s.cover_url,
      description:  '',
      href:         seriesHref(s.manga_id),
      badge:        '✓ In Hebrew',
      badgeColor:   'green' as const,
      genres:       heroGenres.get(s.manga_id) ?? [],
      chapterCount: s.chapter_count,
    }))

    if (libSlides.length >= 10) return libSlides

    // Pad with MangaDex popular (genres come directly from their API response)
    const libIds = new Set(librarySeries.map(s => s.manga_id))
    const mdSlides: HeroSlide[] = mdPopular
      .filter(m => !libIds.has(m.id))
      .slice(0, 10 - libSlides.length)
      .map(m => ({
        id:          m.id,
        title:       getMDTitle(m),
        coverUrl:    getMDCover(m),
        description: '',
        href:        `/manga/${m.id}`,
        badge:       'MangaDex',
        badgeColor:  'orange' as const,
        genres:      m.attributes.tags
          .map(t => t.attributes.name['en'])
          .filter(Boolean)
          .slice(0, 6),
      }))

    return [...libSlides, ...mdSlides]
  }, [librarySeries, mdPopular, heroGenres])

  // ── Fetch genres for library hero slides ────────────────────────────────────
  // WC library manga → backend scrapes Tag(s). MD library manga → MD API or mdPopular.

  useEffect(() => {
    if (librarySeries.length === 0) return

    const heroLib = librarySeries.slice(0, 10)

    // WC library manga — backend returns tags[]
    const wcFetches = heroLib
      .filter(s => /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(s.manga_id))
      .map(s =>
        fetch(`/api/weebcentral/series/${s.manga_id}`)
          .then(r => r.json())
          .then(d => ({ id: s.manga_id, genres: (d.tags ?? []) as string[] }))
          .catch(() => ({ id: s.manga_id, genres: [] as string[] }))
      )

    // MD library manga not already in mdPopular
    const mdPopularIds = new Set(mdPopular.map(m => m.id))
    const mdFetches = heroLib
      .filter(s => !/^[0-9A-HJKMNP-TV-Z]{26}$/i.test(s.manga_id) && !mdPopularIds.has(s.manga_id))
      .map(s =>
        fetch(`https://api.mangadex.org/manga/${s.manga_id}`)
          .then(r => r.json())
          .then(d => ({
            id: s.manga_id,
            genres: ((d.data?.attributes?.tags ?? []) as { attributes: { name: Record<string,string> } }[])
              .map(t => t.attributes.name['en']).filter(Boolean).slice(0, 6) as string[],
          }))
          .catch(() => ({ id: s.manga_id, genres: [] as string[] }))
      )

    // MD library manga already in mdPopular — extract inline (no fetch needed)
    const mdPopularMap = new Map(mdPopular.map(m => [
      m.id,
      m.attributes.tags.map(t => t.attributes.name['en']).filter(Boolean).slice(0, 6) as string[],
    ]))

    Promise.all([...wcFetches, ...mdFetches]).then(results => {
      setHeroGenres(prev => {
        const next = new Map(prev)
        // Inline MD popular genres
        heroLib.forEach(s => {
          if (mdPopularMap.has(s.manga_id)) next.set(s.manga_id, mdPopularMap.get(s.manga_id)!)
        })
        // Fetched genres
        results.forEach(r => { if (r.genres.length) next.set(r.id, r.genres) })
        return next
      })
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [librarySeries.length, mdPopular.length])

  // Hero is ready once WC or library data arrives (whichever comes first)
  const heroLoading  = wcLoading && libLoading
  const totalChaps   = libraryChapters.length
  const totalSeries  = librarySeries.length

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen">

      {/* ── Hero carousel ── */}
      <HeroCarousel slides={heroSlides} loading={heroLoading} />

      {/* ── Stats strip ── */}
      {!libLoading && totalChaps > 0 && (
        <div
          className="border-y py-3 px-4 text-center text-xs text-zinc-500"
          style={{ borderColor: 'var(--card-border)', background: 'rgba(255,255,255,0.015)' }}
        >
          <span className="text-zinc-300 font-semibold">{totalChaps}</span> chapters
          {' · '}
          <span className="text-zinc-300 font-semibold">{totalSeries}</span> manga series
          {' · '}translated to{' '}
          <span className="font-semibold" style={{ color: 'var(--accent)' }}>Hebrew</span>
        </div>
      )}

      {/* ── Rows ── */}
      <div className="pt-8">

        {/* Recently Translated */}
        <RowSection
          title="Recently Translated"
          loading={libLoading}
          empty={recentChapters.length === 0}
        >
          {recentChapters.map(ch => (
            <div key={ch.id} className="w-36 sm:w-44 flex-shrink-0">
              <MangaCard
                href={`/library/${ch.id}`}
                title={ch.manga_title}
                coverUrl={ch.cover_url}
                subtitle={ch.chapter_num ? `Ch. ${ch.chapter_num}` : (ch.chapter_title ?? 'Read')}
              />
            </div>
          ))}
        </RowSection>

        {/* Hebrew Library */}
        <RowSection
          title="Hebrew Library"
          loading={libLoading}
          empty={librarySeries.length === 0}
        >
          {/* "+" add card */}
          <div className="w-36 sm:w-44 flex-shrink-0 self-stretch">
            <Link href="/discover" className="group block h-full">
              <div
                className="rounded-xl border-2 border-dashed flex flex-col items-center justify-center gap-2 text-zinc-600 group-hover:text-[var(--accent)] group-hover:bg-[var(--accent-subtle)] transition-all duration-200 h-full"
                style={{ borderColor: 'var(--card-border)', minHeight: '220px' }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--accent)')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--card-border)')}
              >
                <span className="text-4xl leading-none group-hover:scale-110 transition-transform duration-200">＋</span>
                <span className="text-xs font-medium">Add Manga</span>
              </div>
            </Link>
          </div>

          {librarySeries.map(s => {
            const src = getSource(s.manga_id)
            return (
              <div key={s.manga_id} className="w-36 sm:w-44 flex-shrink-0">
                <MangaCard
                  href={seriesHref(s.manga_id)}
                  title={s.manga_title}
                  coverUrl={s.cover_url}
                  subtitle={`${s.chapter_count} ch. in Hebrew`}
                  badge={src === 'weebcentral' ? 'WeebCentral' : 'MangaDex'}
                  badgeColor={src === 'weebcentral' ? 'violet' : 'orange'}
                />
              </div>
            )
          })}
        </RowSection>

        {/* Continue Reading */}
        {continueReading.length > 0 && (
          <RowSection title="History">
            {continueReading.map(p => (
              <div key={p.manga_id} className="w-36 sm:w-44 flex-shrink-0">
                <MangaCard
                  href={`/library/${p.chapter_id}`}
                  title={p.manga_title}
                  coverUrl={p.cover_url}
                  subtitle={p.chapter_num ? `Ch. ${p.chapter_num}` : 'Resume'}
                />
              </div>
            ))}
          </RowSection>
        )}

        {/* WeebCentral Trending */}
        <RowSection
          title="Trending on WeebCentral"
          href="/discover"
          hrefLabel="Discover →"
          loading={wcLoading}
          empty={wcFeatured.length === 0}
        >
          {wcFeatured.map(m => (
            <div key={m.id} className="w-36 sm:w-44 flex-shrink-0">
              <MangaCard
                href={`/weebcentral/${m.id}`}
                title={m.title}
                coverUrl={m.cover || null}
              />
            </div>
          ))}
        </RowSection>

        {/* MangaDex Popular */}
        <RowSection
          title="Popular on MangaDex"
          href="/discover"
          hrefLabel="Discover →"
          loading={mdLoading}
          empty={mdPopular.length === 0}
        >
          {mdPopular.map(m => (
            <div key={m.id} className="w-36 sm:w-44 flex-shrink-0">
              <MangaCard
                href={`/manga/${m.id}`}
                title={getMDTitle(m)}
                coverUrl={getMDCover(m)}
              />
            </div>
          ))}
        </RowSection>

      </div>

      <p className="text-center text-xs text-zinc-700 pb-10 mt-2">
        HeManga — מנגה בעברית
      </p>
    </main>
  )
}
