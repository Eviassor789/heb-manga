'use client'

import { useEffect, useState } from 'react'
import {
  getGeminiKey, setGeminiKey, clearGeminiKey, hasGeminiKey,
  getModalTokens, setModalTokens, clearModalTokens,
  hasModalTokens, isModalDeployed, markModalDeployed,
} from '@/lib/apiKeys'

type SetupStatus = 'idle' | 'deploying' | 'connected' | 'error'

// ── Tutorial steps for Modal ──────────────────────────────────────────────────

const MODAL_STEPS = [
  { n: 1, before: 'Go to ', link: { label: 'modal.com', href: 'https://modal.com' }, after: ' and create a free account (takes ~30 seconds).' },
  { n: 2, before: 'After signing in, click your avatar → ', link: { label: 'Settings', href: 'https://modal.com/settings' }, after: null },
  { n: 3, before: 'Open the ', link: { label: 'API Tokens', href: 'https://modal.com/settings' }, after: ' tab.' },
  { n: 4, before: 'Click ', code: '"New Token"', after: ' — give it any name, e.g. "HeManga".' },
  { n: 5, before: 'Copy the ', code: 'Token ID', after: ' and ', code2: 'Token Secret', after2: ' that appear.' },
  { n: 6, before: 'Paste them in the fields below and click Connect & Deploy.', link: null, after: null },
]

// ── Component ─────────────────────────────────────────────────────────────────

export default function SettingsPage() {

  // ── Gemini state ──────────────────────────────────────────────────────────
  const [geminiInput,   setGeminiInput]   = useState('')
  const [geminiVisible, setGeminiVisible] = useState(false)
  const [geminiSaved,   setGeminiSaved]   = useState(false)
  const [geminiHasKey,  setGeminiHasKey]  = useState(false)

  // ── Modal GPU state ───────────────────────────────────────────────────────
  const [modalTab,    setModalTab]    = useState<'existing' | 'new'>('existing')
  const [tokenId,     setTokenId]     = useState('')
  const [tokenSec,    setTokenSec]    = useState('')
  const [gpuStatus,   setGpuStatus]   = useState<SetupStatus>('idle')
  const [gpuError,    setGpuError]    = useState('')
  const [showSecret,  setShowSecret]  = useState(false)
  const [tokenIdErr,  setTokenIdErr]  = useState('')
  const [tokenSecErr, setTokenSecErr] = useState('')

  // Load from localStorage on mount
  useEffect(() => {
    // Gemini
    setGeminiInput(getGeminiKey())
    setGeminiHasKey(hasGeminiKey())

    // Modal
    const { tokenId: id, tokenSecret: sec } = getModalTokens()
    if (id)  setTokenId(id)
    if (sec) setTokenSec(sec)
    if (id && sec && isModalDeployed()) setGpuStatus('connected')
    else if (id && sec)                 setGpuStatus('idle')
  }, [])

  // ── Gemini handlers ───────────────────────────────────────────────────────
  const saveGemini = () => {
    if (!geminiInput.trim()) return
    setGeminiKey(geminiInput.trim())
    setGeminiHasKey(true)
    setGeminiSaved(true)
    setTimeout(() => setGeminiSaved(false), 2000)
  }

  const clearGemini = () => {
    clearGeminiKey()
    setGeminiInput('')
    setGeminiHasKey(false)
    setGeminiSaved(false)
  }

  // ── Modal GPU handlers ────────────────────────────────────────────────────

  /** Modal Token ID must start with "ak-" followed by alphanumeric chars. */
  const validateTokenId  = (v: string) => /^ak-[A-Za-z0-9]{8,}$/.test(v.trim())
  /** Modal Token Secret must start with "as-" followed by alphanumeric chars. */
  const validateTokenSec = (v: string) => /^as-[A-Za-z0-9]{8,}$/.test(v.trim())

  const isGpuReady = tokenId.trim().length > 0 && tokenSec.trim().length > 0

  /** Returns true if both tokens pass format validation, setting inline errors otherwise. */
  const checkTokenFormat = (): boolean => {
    let ok = true
    if (!validateTokenId(tokenId)) {
      setTokenIdErr('Token ID must start with "ak-" (e.g. ak-AbcXyz…)')
      ok = false
    } else {
      setTokenIdErr('')
    }
    if (!validateTokenSec(tokenSec)) {
      setTokenSecErr('Token Secret must start with "as-" (e.g. as-AbcXyz…)')
      ok = false
    } else {
      setTokenSecErr('')
    }
    return ok
  }

  const connectAndDeploy = async () => {
    if (!isGpuReady) return
    if (!checkTokenFormat()) return
    setGpuStatus('deploying')
    setGpuError('')
    try {
      const res = await fetch('/api/modal/setup', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          modal_token_id:     tokenId.trim(),
          modal_token_secret: tokenSec.trim(),
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `Server error ${res.status}`)
      }
      setModalTokens(tokenId.trim(), tokenSec.trim())
      markModalDeployed()
      setGpuStatus('connected')
    } catch (e: unknown) {
      setGpuError(e instanceof Error ? e.message : String(e))
      setGpuStatus('error')
    }
  }

  const justSaveGpu = () => {
    if (!isGpuReady) return
    if (!checkTokenFormat()) return
    setModalTokens(tokenId.trim(), tokenSec.trim())
    setGpuStatus('connected')
  }

  const disconnectGpu = () => {
    clearModalTokens()
    setTokenId('')
    setTokenSec('')
    setTokenIdErr('')
    setTokenSecErr('')
    setGpuStatus('idle')
    setGpuError('')
  }

  // ── Shared card style ─────────────────────────────────────────────────────
  const cardStyle: React.CSSProperties = {
    background: 'var(--card-bg)',
    border:     '1px solid var(--card-border)',
  }

  const statusBadge = (connected: boolean) => (
    <span
      className="inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded-full"
      style={{
        background: connected ? 'rgba(74,222,128,0.12)' : 'rgba(161,161,170,0.12)',
        color:      connected ? '#4ade80' : '#71717a',
        border:     `1px solid ${connected ? 'rgba(74,222,128,0.3)' : 'rgba(113,113,122,0.3)'}`,
      }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: connected ? '#4ade80' : '#52525b' }} />
      {connected ? 'Configured' : 'Not set'}
    </span>
  )

  return (
    <main className="max-w-2xl mx-auto px-4 py-12">

      <h1 className="text-2xl font-bold text-zinc-100 mb-1">Settings</h1>
      <p className="text-zinc-500 text-sm mb-10">
        All keys are stored locally in this browser — they are never sent to our servers except as per-request headers to their respective APIs.
      </p>

      {/* ══════════════════════════════════════════════════════════════════════
          SECTION 1 — AI Translation (Gemini)
      ══════════════════════════════════════════════════════════════════════ */}
      <section className="rounded-2xl p-6 mb-6" style={cardStyle}>

        {/* Header */}
        <div className="flex items-start justify-between gap-4 mb-5">
          <div>
            <h2 className="text-base font-semibold text-zinc-100 flex items-center gap-2">
              AI Translation
              {statusBadge(geminiHasKey)}
            </h2>
            <p className="text-xs text-zinc-500 mt-1">
              Your{' '}
              <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer"
                 className="underline hover:text-zinc-300 transition-colors">
                Google Gemini
              </a>{' '}
              key is used for OCR (reading the original text) and Hebrew translation.
              Costs a{' '}
              <span className="text-zinc-400">few cents per chapter</span>.
            </p>
          </div>
          <a
            href="https://aistudio.google.com/apikey"
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 text-xs hover:underline transition-colors mt-0.5"
            style={{ color: 'var(--accent)' }}
          >
            Get key ↗
          </a>
        </div>

        {/* Privacy banner */}
        <div
          className="flex items-start gap-2.5 mb-4 px-3 py-2.5 rounded-xl"
          style={{ background: 'rgba(20,83,45,0.35)', border: '1px solid rgba(34,197,94,0.2)' }}
        >
          <span className="text-green-400 shrink-0 mt-0.5 text-sm">🔒</span>
          <p className="text-xs text-green-300 leading-relaxed">
            <span className="font-semibold">Your key never reaches our servers.</span>{' '}
            It is saved in this browser only and sent per-request directly to Google.
          </p>
        </div>

        {/* Billing note */}
        <div
          className="flex items-start gap-2 mb-4 px-2.5 py-2 rounded-lg"
          style={{ background: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)' }}
        >
          <span className="shrink-0 mt-0.5 text-sm">💳</span>
          <p className="text-xs text-yellow-200/80 leading-relaxed">
            <span className="font-semibold text-yellow-200">Enable billing on your Google AI project</span>{' '}
            — the API requires it. Usually under{' '}
            <span className="font-semibold text-yellow-200">₪0.20 per chapter</span>.
          </p>
        </div>

        {/* Input */}
        <div className="relative mb-3">
          <input
            type={geminiVisible ? 'text' : 'password'}
            className="input pr-16 font-mono text-sm w-full"
            placeholder="AIza…"
            value={geminiInput}
            onChange={e => { setGeminiInput(e.target.value); setGeminiSaved(false) }}
            onKeyDown={e => e.key === 'Enter' && geminiInput.trim() && saveGemini()}
            spellCheck={false}
            autoComplete="off"
          />
          <button
            type="button"
            onClick={() => setGeminiVisible(v => !v)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
            style={{ minHeight: 'auto' }}
          >
            {geminiVisible ? 'Hide' : 'Show'}
          </button>
        </div>

        {/* Save / clear row */}
        <div className="flex items-center gap-3">
          <button
            onClick={saveGemini}
            disabled={!geminiInput.trim()}
            className="btn-primary text-sm px-5 py-2"
          >
            {geminiSaved ? '✓ Saved!' : 'Save Key'}
          </button>
          {geminiHasKey && (
            <button
              onClick={clearGemini}
              className="text-xs text-red-500 hover:text-red-400 transition-colors"
              style={{ minHeight: 'auto' }}
            >
              Clear
            </button>
          )}
          {geminiHasKey && !geminiSaved && (
            <span className="text-xs ml-auto" style={{ color: '#4ade80' }}>✓ Key saved</span>
          )}
        </div>

      </section>

      {/* ══════════════════════════════════════════════════════════════════════
          SECTION 2 — GPU Acceleration (Modal)
      ══════════════════════════════════════════════════════════════════════ */}
      <section className="rounded-2xl p-6" style={cardStyle}>

        {/* Header */}
        <div className="flex items-start justify-between gap-4 mb-5">
          <div>
            <h2 className="text-base font-semibold text-zinc-100 flex items-center gap-2">
              GPU Acceleration
              <span
                className="inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded-full"
                style={{
                  background: gpuStatus === 'connected' ? 'rgba(74,222,128,0.12)' : 'rgba(161,161,170,0.12)',
                  color:      gpuStatus === 'connected' ? '#4ade80' : '#71717a',
                  border:     `1px solid ${gpuStatus === 'connected' ? 'rgba(74,222,128,0.3)' : 'rgba(113,113,122,0.3)'}`,
                }}
              >
                <span className="w-1.5 h-1.5 rounded-full"
                      style={{ background: gpuStatus === 'connected' ? '#4ade80' : '#52525b' }} />
                {gpuStatus === 'connected' ? 'Connected' : 'Not connected'}
              </span>
            </h2>
            <p className="text-xs text-zinc-500 mt-1">
              Connect your own{' '}
              <a href="https://modal.com" target="_blank" rel="noopener noreferrer"
                 className="underline hover:text-zinc-300 transition-colors">
                Modal
              </a>{' '}
              account so GPU costs (text detection + inpainting) are billed to you directly.{' '}
              <span className="text-zinc-400">Free tier covers ~1,000 chapters/month.</span>
            </p>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 mb-5 p-1 rounded-lg" style={{ background: 'rgba(0,0,0,0.3)' }}>
          {(['existing', 'new'] as const).map(t => (
            <button
              key={t}
              onClick={() => setModalTab(t)}
              className="flex-1 py-1.5 text-xs font-medium rounded-md transition-all"
              style={{
                background: modalTab === t ? 'var(--card-bg)' : 'transparent',
                color:      modalTab === t ? 'var(--accent)' : '#71717a',
                border:     modalTab === t ? '1px solid var(--card-border-hover)' : '1px solid transparent',
                minHeight:  'auto',
              }}
            >
              {t === 'existing' ? 'I already have keys' : 'I need to get keys'}
            </button>
          ))}
        </div>

        {/* Tutorial tab */}
        {modalTab === 'new' && (
          <ol className="space-y-3 mb-5">
            {MODAL_STEPS.map(step => (
              <li key={step.n} className="flex gap-3">
                <span
                  className="shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold"
                  style={{ background: 'var(--accent-subtle)', color: 'var(--accent)',
                           border: '1px solid var(--card-border-hover)' }}
                >
                  {step.n}
                </span>
                <p className="text-sm text-zinc-400 leading-relaxed">
                  {step.before}
                  {'link' in step && step.link && (
                    <a href={step.link.href} target="_blank" rel="noopener noreferrer"
                       className="text-[var(--accent)] hover:underline">
                      {step.link.label}
                    </a>
                  )}
                  {'code' in step && step.code && (
                    <span className="font-semibold text-zinc-200">{step.code}</span>
                  )}
                  {step.after}
                  {'code2' in step && step.code2 && (
                    <span className="font-semibold text-zinc-200">{step.code2}</span>
                  )}
                  {'after2' in step && step.after2}
                </p>
              </li>
            ))}
          </ol>
        )}

        {/* Inputs */}
        <div className="space-y-3 mb-5">
          <div>
            <label className="block text-xs font-medium text-zinc-400 mb-1">Token ID</label>
            <input
              type="text"
              placeholder="ak-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              value={tokenId}
              onChange={e => { setTokenId(e.target.value); setGpuStatus('idle'); setTokenIdErr('') }}
              className={`w-full input text-sm font-mono ${tokenIdErr ? 'border-red-600' : ''}`}
              spellCheck={false}
              autoComplete="off"
            />
            {tokenIdErr && (
              <p className="mt-1 text-xs text-red-400">{tokenIdErr}</p>
            )}
          </div>
          <div>
            <label className="block text-xs font-medium text-zinc-400 mb-1">Token Secret</label>
            <div className="relative">
              <input
                type={showSecret ? 'text' : 'password'}
                placeholder="as-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                value={tokenSec}
                onChange={e => { setTokenSec(e.target.value); setGpuStatus('idle'); setTokenSecErr('') }}
                className={`w-full input text-sm font-mono pr-16 ${tokenSecErr ? 'border-red-600' : ''}`}
                spellCheck={false}
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => setShowSecret(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
                style={{ minHeight: 'auto' }}
              >
                {showSecret ? 'Hide' : 'Show'}
              </button>
            </div>
            {tokenSecErr && (
              <p className="mt-1 text-xs text-red-400">{tokenSecErr}</p>
            )}
          </div>
        </div>

        {/* Error */}
        {gpuStatus === 'error' && gpuError && (
          <div
            className="rounded-lg px-4 py-3 mb-4 text-xs text-red-300 font-mono whitespace-pre-wrap break-all"
            style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)' }}
          >
            {gpuError}
          </div>
        )}

        {/* Actions */}
        {gpuStatus === 'connected' ? (
          <div className="flex items-center gap-3">
            <span className="text-sm text-[#4ade80] flex items-center gap-2">
              <span>✓</span> GPU connected — rendering costs go to your Modal account.
            </span>
            <button onClick={disconnectGpu} className="ml-auto btn-ghost text-xs"
                    style={{ minHeight: 'auto' }}>
              Disconnect
            </button>
          </div>
        ) : (
          <div className="flex flex-col sm:flex-row gap-2">
            <button
              onClick={connectAndDeploy}
              disabled={!isGpuReady || gpuStatus === 'deploying'}
              className="flex-1 btn-primary text-sm flex items-center justify-center gap-2"
            >
              {gpuStatus === 'deploying' ? (
                <>
                  <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10"
                            stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor"
                          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Deploying… (~45 s)
                </>
              ) : 'Connect & Deploy'}
            </button>
            <button
              onClick={justSaveGpu}
              disabled={!isGpuReady || gpuStatus === 'deploying'}
              className="sm:w-auto btn-ghost text-xs px-4"
              style={{ minHeight: 'auto' }}
              title="Use if you already ran modal deploy manually"
            >
              Just save keys
            </button>
          </div>
        )}

        {gpuStatus !== 'connected' && (
          <p className="text-[11px] text-zinc-600 mt-3 leading-relaxed">
            <span className="font-semibold text-zinc-500">Connect & Deploy</span> uploads the GPU
            code to your Modal workspace once (~45 s).{' '}
            <span className="font-semibold text-zinc-500">Just save keys</span> skips the deploy —
            only use it if you already ran{' '}
            <code className="font-mono text-zinc-400">modal deploy modal_gpu.py</code> yourself.
          </p>
        )}

      </section>

    </main>
  )
}
