/**
 * Rewrite a stored MangaDex cover URL to go through our server-side proxy.
 *
 * Chapters translated from MangaDex have their `cover_url` persisted (in the
 * library DB) as a direct `https://uploads.mangadex.org/covers/...` URL,
 * captured at translation time. uploads.mangadex.org's hotlink protection
 * blocks <img> requests whose Referer isn't mangadex.org, so any direct use
 * of that URL in the browser serves the "read this on mangadex.org"
 * placeholder instead of the real cover.
 *
 * Route it through /api/mangadex-cdn/ instead — that route fetches the cover
 * server-side with the correct Referer header.
 */
export function proxiedCoverUrl(url: string | null | undefined): string | null {
  if (!url) return url ?? null
  const prefix = 'https://uploads.mangadex.org/'
  if (url.startsWith(prefix)) return `/api/mangadex-cdn/${url.slice(prefix.length)}`
  return url
}
