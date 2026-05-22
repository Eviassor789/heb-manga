import Link from 'next/link'
import MangaCover from './MangaCover'

interface MangaCardProps {
  href:        string
  title:       string
  coverUrl?:   string | null
  subtitle?:   string           // e.g. "5 chapters" or "Chapter 12"
  badge?:      string           // e.g. "✓ In Library"
  badgeColor?: 'green' | 'violet'
  external?:   boolean          // open in a new browser tab (for external URLs)
}

/**
 * Reusable manga card — cover image + title + optional subtitle + optional badge.
 * Uses the global .manga-card class for violet-glow hover effects.
 */
export default function MangaCard({
  href, title, coverUrl, subtitle, badge, badgeColor = 'green', external = false,
}: MangaCardProps) {
  const badgeCls = badgeColor === 'green' ? 'badge-green' : 'badge-violet'

  const inner = (
    <div className="manga-card">
      {/* Cover */}
      <div className="relative">
        <MangaCover src={coverUrl} alt={title} className="rounded-t-xl" />
        {badge && (
          <div className={`absolute top-2 right-2 ${badgeCls}`}>{badge}</div>
        )}
      </div>

      {/* Info */}
      <div className="p-3">
        <p className="text-sm font-semibold text-zinc-100 line-clamp-2 leading-snug group-hover:text-white transition-colors">
          {title}
        </p>
        {subtitle && (
          <p className="text-xs text-zinc-500 mt-1">{subtitle}</p>
        )}
      </div>
    </div>
  )

  if (external) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className="group block">
        {inner}
      </a>
    )
  }

  return (
    <Link href={href} className="group block">
      {inner}
    </Link>
  )
}
