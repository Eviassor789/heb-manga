'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import ApiKeyModal from '@/components/ApiKeyModal'
import { hasGeminiKey } from '@/lib/apiKeys'

const LINKS = [
  { href: '/',          label: 'Home',     icon: '🏠' },
  { href: '/discover',  label: 'Discover', icon: '🔍' },
  { href: '/translate', label: 'Upload',   icon: '⬆'  },
]

export default function NavBar() {
  const pathname = usePathname()
  const router   = useRouter()
  const [query,      setQuery]      = useState('')
  const [modalOpen,  setModalOpen]  = useState(false)
  const [hasKey,     setHasKey]     = useState(false)

  // Read key status client-side (localStorage not available during SSR)
  useEffect(() => {
    setHasKey(hasGeminiKey())
  }, [])

  const refreshKeyStatus = () => setHasKey(hasGeminiKey())

  // Full-screen reader: hide the global nav entirely
  if (pathname.startsWith('/library/')) return null

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (query.trim()) {
      router.push(`/discover?q=${encodeURIComponent(query.trim())}`)
      setQuery('')
    }
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

            {/* Search — desktop */}
            <form onSubmit={handleSearch} className="hidden sm:block">
              <input
                className="bg-zinc-800/70 border border-zinc-700 rounded-lg px-3 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-[var(--accent)] w-36 lg:w-48 transition-all"
                placeholder="Search manga…"
                value={query}
                onChange={e => setQuery(e.target.value)}
              />
            </form>

            {/* Search icon — mobile */}
            <Link
              href="/discover"
              className="sm:hidden flex items-center justify-center w-8 h-8 rounded-lg text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/50 transition-all text-sm"
            >
              🔍
            </Link>

            {/* API Key button */}
            <button
              onClick={() => setModalOpen(true)}
              className="relative flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
              style={{ border: '1px solid var(--card-border)' }}
              title={hasKey ? 'API key configured — click to update' : 'Set your Gemini API key'}
            >
              {/* Status dot */}
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
      </nav>

      <ApiKeyModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSave={refreshKeyStatus}
      />
    </>
  )
}
