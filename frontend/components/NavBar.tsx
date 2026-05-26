'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import ApiKeyModal from '@/components/ApiKeyModal'
import { hasGeminiKey } from '@/lib/apiKeys'

const LINKS = [
  { href: '/',          label: 'Home',     icon: '🏠' },
  { href: '/discover',  label: 'Discover', icon: '🔍' },
  { href: '/translate', label: 'Upload',   icon: '⬆'  },
]

function SearchIcon({ className = '' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none"
      stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8.5" cy="8.5" r="5.5" />
      <line x1="12.5" y1="12.5" x2="17" y2="17" />
    </svg>
  )
}

export default function NavBar() {
  const pathname = usePathname()
  const router   = useRouter()

  const [query,            setQuery]            = useState('')
  const [modalOpen,        setModalOpen]        = useState(false)
  const [hasKey,           setHasKey]           = useState(false)
  const [mobileSearchOpen, setMobileSearchOpen] = useState(false)

  const mobileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { setHasKey(hasGeminiKey()) }, [])

  // Auto-focus the mobile search input when it opens
  useEffect(() => {
    if (mobileSearchOpen) mobileInputRef.current?.focus()
  }, [mobileSearchOpen])

  // Close mobile search when navigating away
  useEffect(() => { setMobileSearchOpen(false) }, [pathname])

  const refreshKeyStatus = () => setHasKey(hasGeminiKey())

  // Full-screen reader: hide nav entirely
  if (pathname.startsWith('/library/')) return null

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return
    router.push(`/discover?q=${encodeURIComponent(query.trim())}`)
    setQuery('')
    setMobileSearchOpen(false)
  }

  return (
    <>
      <nav className="sticky top-0 z-40 backdrop-blur-md border-b border-[var(--card-border)] bg-[rgba(9,9,15,0.92)]">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between relative">

          {/* Logo — left */}
          <Link href="/" className="flex items-center gap-2 shrink-0 z-10">
            <span className="text-xl">🈺</span>
            <span className="text-sm font-bold text-[var(--accent)] hidden sm:block tracking-wide">
              HeManga
            </span>
          </Link>

          {/* Nav links — truly centered via absolute positioning */}
          <div className="absolute left-1/2 -translate-x-1/2 flex items-center gap-0.5">
            {LINKS.map(({ href, label, icon }) => {
              const active = href === '/' ? pathname === '/' : pathname.startsWith(href)
              return (
                <Link
                  key={href}
                  href={href}
                  className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
                    active
                      ? 'text-pink-soft font-semibold'
                      : 'text-zinc-500 hover:text-pink-soft'
                  }`}
                >
                  <span>{icon}</span>
                  <span className="hidden sm:block">{label}</span>
                </Link>
              )
            })}
          </div>

          {/* Search + API key — right */}
          <div className="flex items-center gap-2 shrink-0 z-10">

            {/* Search bar — desktop */}
            <form onSubmit={handleSearch} className="hidden sm:flex items-center relative">
              <input
                className="bg-zinc-800/70 border border-zinc-700 rounded-lg pl-3 pr-8 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-[var(--accent)] w-36 lg:w-48 transition-all"
                placeholder="Search manga…"
                value={query}
                onChange={e => setQuery(e.target.value)}
              />
              <button
                type="submit"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-[var(--accent)] transition-colors"
                aria-label="Search"
              >
                <SearchIcon className="w-3.5 h-3.5" />
              </button>
            </form>

            {/* Search icon button — mobile (toggles the slide-down bar) */}
            <button
              onClick={() => setMobileSearchOpen(v => !v)}
              className={`sm:hidden flex items-center justify-center w-8 h-8 rounded-lg transition-all ${
                mobileSearchOpen
                  ? 'text-[var(--accent)] bg-zinc-800/60'
                  : 'text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50'
              }`}
              aria-label="Toggle search"
            >
              <SearchIcon className="w-4 h-4" />
            </button>

            {/* API Key button */}
            <button
              onClick={() => setModalOpen(true)}
              className="relative flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
              style={{ border: '1px solid var(--card-border)' }}
              title={hasKey ? 'API key configured — click to update' : 'Set your Gemini API key'}
            >
              <span
                className="w-1.5 h-1.5 rounded-full shrink-0"
                style={{ background: hasKey ? '#4ade80' : '#f87171' }}
              />
              <span className="hidden sm:block" style={{ color: hasKey ? '#4ade80' : '#f87171' }}>
                {hasKey ? 'Key set' : 'Add key'}
              </span>
              <span className="sm:hidden text-zinc-400">🔑</span>
            </button>
          </div>

        </div>

        {/* Mobile search bar — slides in below the nav row */}
        {mobileSearchOpen && (
          <div className="sm:hidden border-t border-[var(--card-border)] px-4 py-2.5 animate-fade-in">
            <form onSubmit={handleSearch} className="flex items-center gap-2">
              <div className="relative flex-1">
                <SearchIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-zinc-500 pointer-events-none" />
                <input
                  ref={mobileInputRef}
                  className="w-full bg-zinc-800/70 border border-zinc-700 rounded-lg pl-8 pr-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-[var(--accent)]"
                  placeholder="Search manga…"
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                />
              </div>
              <button type="submit" className="btn-primary px-4 py-2 text-sm shrink-0">
                Search
              </button>
            </form>
          </div>
        )}
      </nav>

      <ApiKeyModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSave={refreshKeyStatus}
      />
    </>
  )
}
