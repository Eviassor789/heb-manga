'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'

const LINKS = [
  { href: '/',          label: 'Library',  icon: '📚' },
  { href: '/discover',  label: 'Discover', icon: '🔍' },
  { href: '/translate', label: 'Upload',   icon: '⬆'  },
]

export default function NavBar() {
  const pathname = usePathname()

  return (
    <nav className="sticky top-0 z-40 backdrop-blur-md border-b border-[var(--card-border)] bg-[rgba(9,9,15,0.88)]">
      <div className="max-w-6xl mx-auto px-4 h-14 flex items-center justify-between gap-6">

        {/* Logo */}
        <Link href="/" className="flex items-center gap-2 shrink-0">
          <span className="text-xl">🈺</span>
          <span className="text-sm font-bold text-[var(--accent)] hidden sm:block tracking-wide">
            Hebrew Manga
          </span>
        </Link>

        {/* Nav links */}
        <div className="flex items-center gap-1">
          {LINKS.map(({ href, label, icon }) => {
            const active =
              href === '/' ? pathname === '/' : pathname.startsWith(href)
            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
                  active
                    ? 'bg-[var(--accent-subtle)] text-white border border-[var(--card-border-hover)]'
                    : 'text-zinc-500 hover:text-zinc-100 hover:bg-zinc-800/50'
                }`}
              >
                <span>{icon}</span>
                <span className="hidden sm:block">{label}</span>
              </Link>
            )
          })}
        </div>
      </div>
    </nav>
  )
}
