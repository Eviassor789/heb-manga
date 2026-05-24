'use client'

import { useState } from 'react'

interface MangaCoverProps {
  src:       string | null | undefined
  alt:       string
  className?: string
}

/**
 * Manga cover image — fixed 3:4 aspect ratio, object-cover, emoji fallback.
 * Used inside MangaCard and the manga detail header.
 */
export default function MangaCover({ src, alt, className = '' }: MangaCoverProps) {
  const [failed, setFailed] = useState(false)

  return (
    <div className={`relative w-full aspect-[2/3] bg-zinc-900 overflow-hidden ${className}`}>
      {src && !failed ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={alt}
          className="absolute inset-0 w-full h-full object-cover"
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center text-4xl text-zinc-700 select-none">
          📖
        </div>
      )}
    </div>
  )
}
