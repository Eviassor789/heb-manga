/**
 * User API key helpers — keys live only in localStorage, never on the server.
 *
 * The user provides their own Gemini API key so OCR and translation run on
 * their own Google quota. GPU processing, storage, and hosting are server costs.
 *
 * Every pipeline-triggering fetch passes the key as X-Gemini-Api-Key.
 * The backend reads it, writes it to job_config.json (per job directory),
 * and uses it for that job instead of the server's .env key.
 * The key is never stored in any database and is cleaned up with the job folder.
 */

const GEMINI_KEY = 'hemanga-gemini-key'

export function getGeminiKey(): string {
  if (typeof window === 'undefined') return ''
  return localStorage.getItem(GEMINI_KEY) ?? ''
}

export function setGeminiKey(key: string): void {
  if (typeof window === 'undefined') return
  if (key.trim()) localStorage.setItem(GEMINI_KEY, key.trim())
  else localStorage.removeItem(GEMINI_KEY)
}

export function clearGeminiKey(): void {
  if (typeof window === 'undefined') return
  localStorage.removeItem(GEMINI_KEY)
}

export function hasGeminiKey(): boolean {
  return !!getGeminiKey()
}

/** Returns headers to attach to every pipeline-triggering fetch. */
export function getApiHeaders(): Record<string, string> {
  const key = getGeminiKey()
  return key ? { 'X-Gemini-Api-Key': key } : {}
}
