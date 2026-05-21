'use client'

import { useEffect, useRef, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'

// ── Types ─────────────────────────────────────────────────────────────────────

type StageKey = 'download' | 'detect' | 'ocr' | 'inpaint' | 'translate' | 'typeset' | 'done'
type StageStatus = 'waiting' | 'running' | 'done' | 'error'

interface StageState {
  status: StageStatus
  page?: number
  total?: number
  message?: string
}

interface StageInfo {
  label: string
  description: string
  icon: string
}

// ── Stage metadata ────────────────────────────────────────────────────────────

const STAGE_ORDER: StageKey[] = ['download', 'detect', 'ocr', 'inpaint', 'translate', 'typeset']

const STAGE_INFO: Record<string, StageInfo> = {
  download:  { label: 'Download',   description: 'Fetching pages from MangaDex CDN',        icon: '⬇️' },
  detect:    { label: 'Detect',     description: 'Locating speech bubbles & text regions',   icon: '🔍' },
  ocr:       { label: 'OCR',        description: 'Extracting Japanese text from each panel',  icon: '📖' },
  inpaint:   { label: 'Inpaint',    description: 'Erasing original text with LaMa AI',        icon: '🎨' },
  translate: { label: 'Translate',  description: 'Translating to Hebrew with Gemini',         icon: '🌐' },
  typeset:   { label: 'Typeset',    description: 'Rendering Hebrew text with bidi support',   icon: '✍️' },
}

// ── Initial state helper ──────────────────────────────────────────────────────

function makeInitialStages(): Record<string, StageState> {
  return Object.fromEntries(STAGE_ORDER.map((k) => [k, { status: 'waiting' as StageStatus }]))
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function JobPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()

  const [stages, setStages] = useState<Record<string, StageState>>(makeInitialStages)
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [chapterTitle, setChapterTitle] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [done, setDone] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!id) return

    const es = new EventSource(`/api/jobs/${id}/status`)
    esRef.current = es
    setConnected(true)

    es.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as {
          stage: string
          status?: string
          page?: number
          total?: number
          message?: string
          download_url?: string
          chapter_title?: string
        }

        const { stage, status, page, total, message, download_url, chapter_title } = data

        if (chapter_title) setChapterTitle(chapter_title)

        if (stage === 'done') {
          // Mark last real stage done, set download url
          setStages((prev) => {
            const next = { ...prev }
            STAGE_ORDER.forEach((k) => {
              if (next[k].status === 'running') next[k] = { status: 'done' }
            })
            return next
          })
          if (download_url) setDownloadUrl(download_url)
          setDone(true)
          es.close()
          return
        }

        if (stage === 'error') {
          setError(message ?? 'An unknown error occurred.')
          setStages((prev) => {
            const next = { ...prev }
            STAGE_ORDER.forEach((k) => {
              if (next[k].status === 'running') next[k] = { ...next[k], status: 'error', message }
            })
            return next
          })
          es.close()
          return
        }

        // Normal stage progress event
        setStages((prev) => {
          const next = { ...prev }

          // Mark previously running stages as done (stage transition)
          STAGE_ORDER.forEach((k) => {
            if (k !== stage && next[k].status === 'running') {
              next[k] = { status: 'done' }
            }
          })

          const currentStatus: StageStatus =
            status === 'done' ? 'done' : status === 'error' ? 'error' : 'running'

          next[stage] = { status: currentStatus, page, total, message }
          return next
        })
      } catch {
        // ignore parse errors
      }
    }

    es.onerror = () => {
      setConnected(false)
      // Don't set error — SSE naturally closes when job stream ends on server
    }

    return () => {
      es.close()
    }
  }, [id])

  // ── Derived state ─────────────────────────────────────────────────────────

  const currentStageKey = STAGE_ORDER.find((k) => stages[k].status === 'running') ?? null
  const hasStarted = STAGE_ORDER.some((k) => stages[k].status !== 'waiting')

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen flex flex-col items-center px-4 py-16">

      {/* Back link */}
      <div className="w-full max-w-2xl mb-8">
        <button
          onClick={() => router.push('/')}
          className="text-zinc-500 hover:text-zinc-300 text-sm transition-colors flex items-center gap-1"
        >
          ← New translation
        </button>
      </div>

      {/* Header */}
      <div className="w-full max-w-2xl mb-8 animate-fade-in">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-zinc-50">
              {done ? 'Translation Complete' : error ? 'Translation Failed' : 'Translating…'}
            </h1>
            {chapterTitle && (
              <p className="text-zinc-400 text-sm mt-1">{chapterTitle}</p>
            )}
            <p className="text-zinc-600 text-xs mt-1 font-mono">{id}</p>
          </div>

          {/* Status badge */}
          <div className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-semibold border ${
            done
              ? 'bg-green-950/50 border-green-700 text-green-400'
              : error
              ? 'bg-red-950/50 border-red-800 text-red-400'
              : 'bg-blue-950/50 border-blue-800 text-blue-400'
          }`}>
            {done ? '✓ Done' : error ? '✗ Error' : connected ? '⟳ Running' : '○ Connecting…'}
          </div>
        </div>
      </div>

      {/* Stage timeline */}
      <div className="w-full max-w-2xl animate-slide-in">
        <div className="card p-6 space-y-1">
          {STAGE_ORDER.map((key, idx) => {
            const info = STAGE_INFO[key]
            const state = stages[key]
            const isLast = idx === STAGE_ORDER.length - 1

            return (
              <div key={key}>
                <StageRow stageKey={key} info={info} state={state} />
                {!isLast && (
                  <div className={`ml-6 w-px h-4 mx-auto transition-colors duration-500 ${
                    state.status === 'done' ? 'bg-green-700' : 'bg-zinc-800'
                  }`} style={{ marginLeft: '1.75rem' }} />
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Error panel */}
      {error && (
        <div className="w-full max-w-2xl mt-6 animate-fade-in">
          <div className="px-5 py-4 bg-red-950/40 border border-red-800 rounded-2xl">
            <p className="text-red-400 text-sm font-medium mb-1">Pipeline error</p>
            <p className="text-red-300 text-sm font-mono whitespace-pre-wrap break-words">{error}</p>
            <button
              onClick={() => router.push('/')}
              className="mt-4 btn-ghost text-sm px-4 py-2"
            >
              ← Try again
            </button>
          </div>
        </div>
      )}

      {/* Download panel */}
      {done && downloadUrl && (
        <div className="w-full max-w-2xl mt-6 animate-fade-in">
          <div className="card p-6 flex flex-col sm:flex-row items-center gap-4">
            <div className="text-4xl">🎉</div>
            <div className="flex-1 text-center sm:text-left">
              <p className="font-semibold text-zinc-100">Your translated manga is ready!</p>
              <p className="text-sm text-zinc-400 mt-0.5">
                All pages have been translated to Hebrew and assembled into a PDF.
              </p>
            </div>
            <a
              href={downloadUrl}
              download
              className="btn-primary shrink-0 flex items-center gap-2 no-underline"
            >
              <span>⬇</span>
              Download PDF
            </a>
          </div>
        </div>
      )}

      {/* Waiting state — show skeleton hint */}
      {!hasStarted && !error && (
        <div className="w-full max-w-2xl mt-6 text-center animate-fade-in">
          <p className="text-zinc-600 text-sm">Connecting to job stream…</p>
        </div>
      )}

      {/* Footer */}
      <p className="mt-12 text-xs text-zinc-700 text-center">
        Powered by comic-text-detector · EasyOCR · LaMa · Gemini · python-bidi
      </p>
    </main>
  )
}

// ── StageRow sub-component ────────────────────────────────────────────────────

function StageRow({
  stageKey,
  info,
  state,
}: {
  stageKey: string
  info: StageInfo
  state: StageState
}) {
  const { status, page, total, message } = state

  const iconBg =
    status === 'done'
      ? 'bg-green-900/60 border-green-700 text-green-400'
      : status === 'running'
      ? 'bg-blue-900/60 border-blue-700 text-blue-300 animate-pulse-fast'
      : status === 'error'
      ? 'bg-red-900/60 border-red-700 text-red-400'
      : 'bg-zinc-800 border-zinc-700 text-zinc-600'

  const labelColor =
    status === 'done'
      ? 'text-zinc-200'
      : status === 'running'
      ? 'text-zinc-100'
      : status === 'error'
      ? 'text-red-400'
      : 'text-zinc-500'

  const descColor =
    status === 'running' ? 'text-zinc-400' : status === 'done' ? 'text-zinc-500' : 'text-zinc-700'

  return (
    <div className={`flex items-start gap-4 p-3 rounded-xl transition-all duration-300 ${
      status === 'running' ? 'bg-zinc-800/60' : ''
    }`}>
      {/* Icon circle */}
      <div className={`shrink-0 w-9 h-9 rounded-full border flex items-center justify-center text-base transition-all duration-300 ${iconBg}`}>
        {status === 'done' ? '✓' : status === 'error' ? '✗' : info.icon}
      </div>

      {/* Text content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-2">
          <span className={`text-sm font-semibold transition-colors duration-300 ${labelColor}`}>
            {info.label}
          </span>
          {status === 'running' && page != null && total != null && (
            <span className="text-xs text-zinc-500 tabular-nums shrink-0">
              {page} / {total}
            </span>
          )}
          {status === 'done' && page != null && total != null && (
            <span className="text-xs text-zinc-600 tabular-nums shrink-0">
              {total} pages
            </span>
          )}
        </div>

        <p className={`text-xs mt-0.5 transition-colors duration-300 ${descColor}`}>
          {status === 'error' && message ? message : info.description}
        </p>

        {/* Progress bar */}
        {status === 'running' && page != null && total != null && total > 0 && (
          <div className="mt-2 h-1 bg-zinc-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
              style={{ width: `${Math.min(100, (page / total) * 100)}%` }}
            />
          </div>
        )}
        {status === 'running' && (page == null || total == null) && (
          <div className="mt-2 h-1 bg-zinc-700 rounded-full overflow-hidden">
            <div className="h-full bg-blue-500 rounded-full animate-pulse w-1/3" />
          </div>
        )}
      </div>
    </div>
  )
}
