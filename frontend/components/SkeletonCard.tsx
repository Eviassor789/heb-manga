/**
 * Loading placeholder that matches MangaCard dimensions.
 * Render a grid of these while data is fetching.
 */
export default function SkeletonCard() {
  return (
    <div
      className="rounded-xl overflow-hidden animate-pulse"
      style={{ background: 'var(--card-bg)', border: '1px solid var(--card-border)' }}
    >
      {/* Cover placeholder — 3:4 ratio */}
      <div className="aspect-[3/4] bg-zinc-800/60" />
      {/* Text placeholders */}
      <div className="p-3 space-y-2">
        <div className="h-3 bg-zinc-800/80 rounded w-4/5" />
        <div className="h-2.5 bg-zinc-800/60 rounded w-2/5" />
      </div>
    </div>
  )
}
