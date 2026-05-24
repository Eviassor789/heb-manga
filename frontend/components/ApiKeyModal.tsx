'use client'

import { useEffect, useState } from 'react'
import {
  getGeminiKey, setGeminiKey, clearGeminiKey, hasGeminiKey,
} from '@/lib/apiKeys'

interface ApiKeyModalProps {
  open:       boolean
  onClose:    () => void
  /** Called after save so NavBar (and other callers) can re-check key status. */
  onSave:     () => void
  /**
   * Gate mode: when provided the modal acts as a pre-translate checkpoint.
   * "Save & Translate →" button saves keys then calls onConfirm.
   * Backdrop click is disabled so the user must explicitly Cancel or fill the key.
   */
  onConfirm?: () => void
}

export default function ApiKeyModal({ open, onClose, onSave, onConfirm }: ApiKeyModalProps) {
  const isGate = !!onConfirm

  const [geminiInput,   setGeminiInput]   = useState('')
  const [geminiVisible, setGeminiVisible] = useState(false)
  const [saved,         setSaved]         = useState(false)

  useEffect(() => {
    if (open) {
      setGeminiInput(getGeminiKey())
      setSaved(false)
      setGeminiVisible(false)
    }
  }, [open])

  if (!open) return null

  const canSave        = !!geminiInput.trim()
  const existingGemini = hasGeminiKey()

  const handleSave = () => {
    setGeminiKey(geminiInput)
    setSaved(true)
    onSave()
    if (onConfirm) {
      onClose()
      onConfirm()
    } else {
      setTimeout(onClose, 800)
    }
  }

  return (
    <>
      {/* Backdrop — no click-to-close in gate mode */}
      <div
        className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm"
        onClick={isGate ? undefined : onClose}
      />

      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          className="pointer-events-auto w-full max-w-md rounded-2xl p-6 animate-fade-in"
          style={{ background: '#111118', border: '1px solid var(--card-border-hover)' }}
          onClick={e => e.stopPropagation()}
        >

          {/* ── Header ── */}
          <div className="flex items-start justify-between mb-4">
            <div>
              <h2 className="text-lg font-bold text-zinc-50">
                {isGate ? '🔑 API Key Required' : 'API Key'}
              </h2>
              <p className="text-xs text-zinc-400 mt-1 leading-relaxed max-w-xs">
                {isGate
                  ? 'Provide your Gemini key to start translating. OCR and translation run on your own quota — we cover the rest.'
                  : 'Your Gemini key is used for OCR and Hebrew translation.'}
              </p>
            </div>
            {!isGate && (
              <button
                onClick={onClose}
                className="ml-4 shrink-0 text-zinc-600 hover:text-zinc-300 transition-colors text-xl leading-none"
              >
                ✕
              </button>
            )}
          </div>

          {/* ── Privacy banner ── */}
          <div
            className="flex items-start gap-2.5 mb-5 px-3 py-2.5 rounded-xl"
            style={{ background: 'rgba(20,83,45,0.35)', border: '1px solid rgba(34,197,94,0.2)' }}
          >
            <span className="text-green-400 shrink-0 mt-0.5">🔒</span>
            <p className="text-xs text-green-300 leading-relaxed">
              <span className="font-semibold">Your key never reaches our servers.</span>{' '}
              It is saved in your browser only and sent per-request to Google — no database, no logs on our end.
            </p>
          </div>

          {/* ── Gemini key ── */}
          <div className="mb-6">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-sm font-semibold text-zinc-100">Google Gemini API Key</span>
              <a
                href="https://aistudio.google.com/apikey"
                target="_blank"
                rel="noopener noreferrer"
                className="ml-auto text-xs hover:underline transition-colors"
                style={{ color: 'var(--pink-soft)' }}
              >
                Get key ↗
              </a>
            </div>

            {/* Billing note */}
            <div
              className="flex items-start gap-2 mb-3 px-2.5 py-2 rounded-lg text-xs"
              style={{ background: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)' }}
            >
              <span className="shrink-0 mt-0.5">💳</span>
              <p className="text-yellow-200/80 leading-relaxed">
                <span className="font-semibold text-yellow-200">Add billing to your Google AI project</span>{' '}
                — the API requires it to function.
                Costs only a <span className="font-semibold text-yellow-200">few cents per chapter</span> (usually under ₪0.20).
              </p>
            </div>

            <div className="relative">
              <input
                type={geminiVisible ? 'text' : 'password'}
                className="input pr-16 font-mono text-sm"
                placeholder="AIza…"
                value={geminiInput}
                onChange={e => { setGeminiInput(e.target.value); setSaved(false) }}
                onKeyDown={e => e.key === 'Enter' && canSave && handleSave()}
                spellCheck={false}
                autoComplete="off"
                // eslint-disable-next-line jsx-a11y/no-autofocus
                autoFocus={isGate && !existingGemini}
              />
              <button
                type="button"
                onClick={() => setGeminiVisible(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
              >
                {geminiVisible ? 'Hide' : 'Show'}
              </button>
            </div>

            {existingGemini && !saved && (
              <div className="flex items-center gap-2 mt-1.5">
                <span className="text-xs" style={{ color: '#4ade80' }}>✓ Key saved</span>
                <button
                  onClick={() => { clearGeminiKey(); setGeminiInput(''); onSave() }}
                  className="text-xs text-red-500 hover:text-red-400 transition-colors"
                >
                  Clear
                </button>
              </div>
            )}
          </div>

          {/* ── Buttons ── */}
          <div className="flex gap-2">
            <button
              onClick={handleSave}
              disabled={!canSave}
              className="btn-primary flex-1 py-2.5 text-sm"
            >
              {saved ? '✓ Saved!' : isGate ? 'Save & Translate →' : 'Save Key'}
            </button>
            <button onClick={onClose} className="btn-ghost px-5 py-2.5 text-sm">
              {isGate ? 'Cancel' : 'Close'}
            </button>
          </div>

          <p className="text-xs text-zinc-600 mt-3 text-center">
            GPU processing and storage are covered by us — you only pay for AI translation.
          </p>

        </div>
      </div>
    </>
  )
}
