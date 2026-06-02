/**
 * User API key helpers — keys live only in localStorage, never on the server.
 *
 * Gemini key  → OCR + translation on the user's own Google quota.
 * Modal tokens → GPU (detect + inpaint) billed to the user's Modal account.
 *
 * Both are sent as request headers on every pipeline-triggering fetch.
 * The backend writes them to job_config.json (per job directory) and uses
 * them for that job.  They are never stored in any database.
 */

// ── Change notification ───────────────────────────────────────────────────────
// Dispatched by every setter/clearer so any component (e.g. NavBar) can react
// immediately without waiting for a page navigation.

function _notifyKeyChange() {
  if (typeof window !== 'undefined')
    window.dispatchEvent(new Event('hemanga-keys-changed'))
}

// ── Gemini ────────────────────────────────────────────────────────────────────

const GEMINI_KEY = 'hemanga-gemini-key'

export function getGeminiKey(): string {
  if (typeof window === 'undefined') return ''
  return localStorage.getItem(GEMINI_KEY) ?? ''
}

export function setGeminiKey(key: string): void {
  if (typeof window === 'undefined') return
  if (key.trim()) localStorage.setItem(GEMINI_KEY, key.trim())
  else localStorage.removeItem(GEMINI_KEY)
  _notifyKeyChange()
}

export function clearGeminiKey(): void {
  if (typeof window === 'undefined') return
  localStorage.removeItem(GEMINI_KEY)
  _notifyKeyChange()
}

export function hasGeminiKey(): boolean {
  return !!getGeminiKey()
}

// ── Modal GPU (BYOK) ──────────────────────────────────────────────────────────

const MODAL_TOKEN_ID_KEY     = 'hemanga-modal-token-id'
const MODAL_TOKEN_SECRET_KEY = 'hemanga-modal-token-secret'
const MODAL_DEPLOYED_KEY     = 'hemanga-modal-deployed'

export function getModalTokens(): { tokenId: string; tokenSecret: string } {
  if (typeof window === 'undefined') return { tokenId: '', tokenSecret: '' }
  return {
    tokenId:     localStorage.getItem(MODAL_TOKEN_ID_KEY)     ?? '',
    tokenSecret: localStorage.getItem(MODAL_TOKEN_SECRET_KEY) ?? '',
  }
}

export function setModalTokens(tokenId: string, tokenSecret: string): void {
  if (typeof window === 'undefined') return
  localStorage.setItem(MODAL_TOKEN_ID_KEY,     tokenId.trim())
  localStorage.setItem(MODAL_TOKEN_SECRET_KEY, tokenSecret.trim())
  _notifyKeyChange()
}

export function clearModalTokens(): void {
  if (typeof window === 'undefined') return
  localStorage.removeItem(MODAL_TOKEN_ID_KEY)
  localStorage.removeItem(MODAL_TOKEN_SECRET_KEY)
  localStorage.removeItem(MODAL_DEPLOYED_KEY)
  _notifyKeyChange()
}

export function hasModalTokens(): boolean {
  const { tokenId, tokenSecret } = getModalTokens()
  return !!(tokenId && tokenSecret)
}

/** Call after a successful /api/modal/setup deploy. */
export function markModalDeployed(): void {
  if (typeof window === 'undefined') return
  localStorage.setItem(MODAL_DEPLOYED_KEY, 'true')
}

/** True once the user has deployed their Modal app at least once from this browser. */
export function isModalDeployed(): boolean {
  if (typeof window === 'undefined') return false
  return localStorage.getItem(MODAL_DEPLOYED_KEY) === 'true'
}

// ── Shared header builder ─────────────────────────────────────────────────────

/** Returns all per-user API headers to attach to every pipeline-triggering fetch. */
export function getApiHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const geminiKey = getGeminiKey()
  if (geminiKey) headers['X-Gemini-Api-Key'] = geminiKey
  const { tokenId, tokenSecret } = getModalTokens()
  if (tokenId && tokenSecret) {
    headers['X-Modal-Token-Id']     = tokenId
    headers['X-Modal-Token-Secret'] = tokenSecret
  }
  return headers
}
