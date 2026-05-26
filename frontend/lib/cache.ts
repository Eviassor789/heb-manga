/**
 * Lightweight in-memory cache for client-side data fetching.
 *
 * Module-level storage persists across Next.js client-side (SPA) navigations
 * because the JS module stays loaded in the browser between routes.  It is
 * intentionally lost on a hard refresh — the cache is purely a navigation-speed
 * optimization, not an offline store.
 *
 * Typical TTLs used across this app
 * ──────────────────────────────────
 *  2 min  — library data (short so new translations appear quickly)
 *  5 min  — search results
 * 10 min  — external featured lists (WC, MangaDex popular)
 * 10 min  — series metadata + chapter lists
 * 30 min  — reader chapter metadata (very stable once translated)
 */

type Entry<T> = { data: T; ts: number }

const _store = new Map<string, Entry<unknown>>()

/**
 * Return a cached value, or null if the entry is absent or older than ttlMs.
 * Default TTL: 5 minutes.
 */
export function cacheGet<T>(key: string, ttlMs = 5 * 60_000): T | null {
  const entry = _store.get(key) as Entry<T> | undefined
  if (!entry) return null
  if (Date.now() - entry.ts > ttlMs) { _store.delete(key); return null }
  return entry.data
}

/** Store a value tagged with the current timestamp. */
export function cacheSet<T>(key: string, data: T): void {
  _store.set(key, { data, ts: Date.now() })
}

/** Immediately remove a specific cache entry (after a mutation, etc.). */
export function cacheInvalidate(key: string): void {
  _store.delete(key)
}

/** Remove all entries whose key starts with the given prefix. */
export function cacheInvalidatePrefix(prefix: string): void {
  for (const key of _store.keys()) {
    if (key.startsWith(prefix)) _store.delete(key)
  }
}
