'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'

// ── Types ─────────────────────────────────────────────────────────────────────

type StageKey = 'download' | 'detect' | 'ocr' | 'inpaint' | 'translate' | 'typeset'
type StageStatus = 'waiting' | 'running' | 'done' | 'error'

interface StageState {
  status: StageStatus
  page?: number
  total?: number
  message?: string
  startedAt?: number
  finishedAt?: number
}

interface ActivityEntry {
  ts: number
  stage: string
  text: string
}

// ── Stage metadata ─────────────────────────────────────────────────────────────

const STAGE_ORDER: StageKey[] = ['download', 'detect', 'ocr', 'inpaint', 'translate', 'typeset']

const STAGE_INFO: Record<string, { label: string; desc: string; icon: string; verb: string }> = {
  download:  { label: 'Download',  icon: '⬇',  desc: 'Fetching pages from MangaDex CDN',       verb: 'Downloading pages' },
  detect:    { label: 'Detect',    icon: '🔍',  desc: 'Locating speech bubbles & text regions',  verb: 'Detecting text regions' },
  ocr:       { label: 'OCR',       icon: '📖',  desc: 'Extracting Japanese text from panels',    verb: 'Reading text' },
  inpaint:   { label: 'Inpaint',   icon: '🎨',  desc: 'Erasing original text with LaMa AI',     verb: 'Erasing text' },
  translate: { label: 'Translate', icon: '🌐',  desc: 'Translating dialogue to Hebrew',          verb: 'Translating' },
  typeset:   { label: 'Typeset',   icon: '✍',  desc: 'Rendering Hebrew text with RTL support',  verb: 'Typesetting' },
}

// The 5 stages that run in parallel per-page (everything after download)
const PIPELINE_STAGES: StageKey[] = ['detect', 'ocr', 'inpaint', 'translate', 'typeset']

// ── Helpers ────────────────────────────────────────────────────────────────────

function makeInitialStages(): Record<string, StageState> {
  return Object.fromEntries(STAGE_ORDER.map(k => [k, { status: 'waiting' as StageStatus }]))
}

function fmt(ms: number) {
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function pct(page?: number, total?: number) {
  if (!page || !total || total === 0) return 0
  return Math.min(100, Math.round((page / total) * 100))
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function JobPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()

  const [stages, setStages] = useState<Record<string, StageState>>(makeInitialStages)
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [failedStage, setFailedStage] = useState<string | null>(null)
  const [chapterTitle, setChapterTitle] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [libraryTimedOut, setLibraryTimedOut] = useState(false)
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const [reconnectKey, setReconnectKey] = useState(0)
  const [resuming, setResuming] = useState(false)
  const [geminiCost, setGeminiCost] = useState<{
    usd: number; ils: number; tokens: { input: number; output: number; think: number; total: number }
  } | null>(null)
  const [modalSeconds, setModalSeconds] = useState<Record<string, number>>({})
  const [libraryId, setLibraryId] = useState<string | null>(null)

  // Modal T4 effective rate — GPU ($0.59/hr) + memory overhead ≈ $1.00/hr total
  const MODAL_T4_USD_PER_SEC = 0.000277
  const ILS_PER_USD = 3.65

  // Wall-clock timer (ticks every second while running)
  const [tick, setTick] = useState(0)
  const jobStartRef    = useRef<number>(Date.now())
  const timerRef       = useRef<ReturnType<typeof setInterval>  | null>(null)
  const fallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const addActivity = useCallback((stage: string, text: string) => {
    setActivity(prev => [...prev.slice(-29), { ts: Date.now(), stage, text }])
  }, [])

  // ── SSE connection ──────────────────────────────────────────────────────────

  useEffect(() => {
    if (!id) return

    jobStartRef.current = Date.now()
    timerRef.current = setInterval(() => setTick(t => t + 1), 1000)

    // Connect directly to FastAPI — Next.js proxies buffer SSE responses
    // (Node.js fetch accumulates the body before streaming it through).
    // CORS is already enabled on the backend for http://localhost:3000.
    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL ?? 'http://localhost:8000'
    const es = new EventSource(`${backendUrl}/api/jobs/${id}/status`)

    es.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as {
          stage: string; status?: string; page?: number; total?: number
          message?: string; download_url?: string; chapter_title?: string
          chapter?: string; total_pages?: number
          cost?: { usd: number; ils: number; tokens: { input: number; output: number; think: number; total: number } }
          modal_gpu_seconds?: number
          library_id?: string
        }
        const { stage, status, page, total, message, download_url, total_pages } = data

        // Library registration complete → navigate straight to the reader
        if (stage === 'library_ready' && data.library_id) {
          // Clear the fallback timer — real event arrived in time
          if (fallbackTimerRef.current) { clearTimeout(fallbackTimerRef.current); fallbackTimerRef.current = null }
          setLibraryId(data.library_id)
          addActivity('done', '📚 Chapter added to library — opening reader…')
          es.close()
          router.push(`/library/${data.library_id}`)
          return
        }

        // Accumulate Gemini costs — both OCR and translate stages emit a `cost` payload
        if (data.cost) {
          const incoming = data.cost
          setGeminiCost(prev => {
            if (!prev) return incoming
            return {
              usd: prev.usd + incoming.usd,
              ils: prev.ils + incoming.ils,
              tokens: {
                input:  prev.tokens.input  + incoming.tokens.input,
                output: prev.tokens.output + incoming.tokens.output,
                think:  prev.tokens.think  + incoming.tokens.think,
                total:  prev.tokens.total  + incoming.tokens.total,
              },
            }
          })
        }
        if (data.modal_gpu_seconds != null && data.modal_gpu_seconds > 0) {
          setModalSeconds(prev => ({ ...prev, [stage]: data.modal_gpu_seconds! }))
        }

        // Backend uses "chapter_title" (new) or "chapter" (legacy) — handle both
        const chapter_title = data.chapter_title || data.chapter
        if (chapter_title) setChapterTitle(chapter_title)

        // ── done ────────────────────────────────────────────────────────────
        if (stage === 'done') {
          setStages(prev => {
            const next = { ...prev }
            // Mark any still-running stages as done (pipeline is fully complete)
            STAGE_ORDER.forEach(k => {
              if (next[k].status === 'running') next[k] = { ...next[k], status: 'done', finishedAt: Date.now() }
            })
            return next
          })
          if (download_url) setDownloadUrl(download_url)
          setDone(true)
          clearInterval(timerRef.current!)
          addActivity('done', '✓ Translation complete — adding to library…')
          // Do NOT close EventSource here — we must keep it open to receive
          // the library_ready event that arrives ~1 second later.
          // Start a 10-second fallback in case library_ready never arrives.
          fallbackTimerRef.current = setTimeout(() => {
            fallbackTimerRef.current = null
            es.close()
            setLibraryTimedOut(true)
            addActivity('done', '⚠ Library registration timed out — download available below')
          }, 10_000)
          return
        }

        // ── error ───────────────────────────────────────────────────────────
        if (stage === 'error') {
          setError(message ?? 'Unknown error')
          setStages(prev => {
            const next = { ...prev }
            let failed = ''
            STAGE_ORDER.forEach(k => {
              if (next[k].status === 'running') { next[k] = { ...next[k], status: 'error', message }; failed = k }
            })
            if (failed) setFailedStage(failed)
            return next
          })
          clearInterval(timerRef.current!)
          addActivity('error', `✗ ${message ?? 'Pipeline error'}`)
          es.close()
          return
        }

        // ── normal stage event ───────────────────────────────────────────────
        const now = Date.now()
        const currentStatus: StageStatus =
          status === 'done' ? 'done' : status === 'error' ? 'error' : 'running'

        setStages(prev => {
          const next = { ...prev }
          // NOTE: Do NOT auto-complete other running stages here.
          // The parallel pipeline allows multiple stages to be running at the
          // same time. Only mark a stage done when the backend sends
          // {status: "done"} for that specific stage.
          const existing = next[stage] ?? { status: 'waiting' }
          next[stage] = {
            ...existing,
            status: currentStatus,
            page:   page ?? existing.page,
            total:  total ?? total_pages ?? existing.total,
            message,
            startedAt: existing.startedAt ?? (currentStatus === 'running' ? now : undefined),
            finishedAt: currentStatus === 'done' ? now : existing.finishedAt,
          }
          return next
        })

        // Activity log entry
        if (page && total) {
          addActivity(stage, `${STAGE_INFO[stage]?.verb ?? stage} — page ${page} / ${total}`)
        } else if (currentStatus === 'done') {
          const t = total ?? total_pages
          addActivity(stage, `✓ ${STAGE_INFO[stage]?.label ?? stage} complete${t ? ` — ${t} pages` : ''}`)
        } else if (currentStatus === 'running' && !page) {
          addActivity(stage, `Starting ${STAGE_INFO[stage]?.label?.toLowerCase() ?? stage}…`)
        }
      } catch { /* ignore parse errors */ }
    }

    es.onerror = () => { /* SSE closes naturally when job stream ends */ }

    return () => {
      es.close()
      clearInterval(timerRef.current!)
      if (fallbackTimerRef.current) { clearTimeout(fallbackTimerRef.current); fallbackTimerRef.current = null }
    }
  }, [id, reconnectKey, addActivity])

  // ── Resume handler ──────────────────────────────────────────────────────────

  const handleResume = async (fromStep: string) => {
    setResuming(true)
    setError(null)
    setFailedStage(null)
    setDone(false)
    setLibraryTimedOut(false)
    setDownloadUrl(null)
    setActivity([])
    setGeminiCost(null)
    setModalSeconds({})
    setLibraryId(null)

    // Reset stages from fromStep onward
    const startIdx = STAGE_ORDER.indexOf(fromStep as StageKey)
    setStages(prev => {
      const next = { ...prev }
      STAGE_ORDER.slice(startIdx).forEach(k => { next[k] = { status: 'waiting' } })
      return next
    })

    try {
      const res = await fetch(`/api/jobs/${id}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from_step: fromStep }),
      })
      if (!res.ok) {
        const d = await res.json()
        setError(d.detail ?? 'Resume failed')
        setResuming(false)
        return
      }
    } catch {
      setError('Could not reach server')
      setResuming(false)
      return
    }

    jobStartRef.current = Date.now()
    timerRef.current = setInterval(() => setTick(t => t + 1), 1000)
    setResuming(false)
    setReconnectKey(k => k + 1) // triggers SSE useEffect
  }

  // ── Derived ─────────────────────────────────────────────────────────────────

  const totalElapsed = Date.now() - jobStartRef.current

  // Total page count — whichever stage first reports it
  const totalPages = stages.typeset?.total ?? stages.translate?.total ?? stages.detect?.total ?? null

  // Pages fully done = typeset.page (last stage — a typeset page is end-to-end complete)
  const pagesFullyDone = stages.typeset?.page ?? 0

  // Overall progress: typeset page fraction is most meaningful (it's the last stage)
  const overallPct = done ? 100 : (() => {
    if (totalPages && totalPages > 0 &&
        (stages.typeset?.status === 'running' || stages.typeset?.status === 'done')) {
      return pct(stages.typeset.page, totalPages)
    }
    // Fall back to stage-completion fraction
    const doneCount = STAGE_ORDER.filter(k => stages[k].status === 'done').length
    return Math.round((doneCount / STAGE_ORDER.length) * 100)
  })()

  const runningStages = STAGE_ORDER.filter(k => stages[k].status === 'running')
  const pipelineActive = PIPELINE_STAGES.some(k => stages[k].status !== 'waiting')
  const isRunning = !done && !error

  const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL ?? 'http://localhost:8000'

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen flex flex-col items-center px-4 py-10">

      {/* Back */}
      <div className="w-full max-w-2xl mb-6">
        <button onClick={() => router.push('/')}
          className="text-zinc-500 hover:text-zinc-300 text-sm transition-colors flex items-center gap-1">
          ← New translation
        </button>
      </div>

      {/* Header card */}
      <div className="w-full max-w-2xl card p-5 mb-4 animate-fade-in">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="min-w-0">
            <h1 className="text-lg font-bold text-zinc-50 truncate">
              {chapterTitle ?? 'Translating…'}
            </h1>
            <p className="text-zinc-600 text-xs font-mono mt-0.5 truncate">{id}</p>
          </div>
          <div className={`shrink-0 px-2.5 py-1 rounded-full text-xs font-semibold border ${
            done  ? 'bg-green-950/60 border-green-700 text-green-400' :
            error ? 'bg-red-950/60 border-red-800 text-red-400' :
                    'bg-blue-950/60 border-blue-800 text-blue-400'
          }`}>
            {done ? '✓ Done' : error ? '✗ Failed' : isRunning ? '⟳ Running' : '○ Idle'}
          </div>
        </div>

        {/* Overall progress bar */}
        <div className="space-y-1.5">
          <div className="flex justify-between text-xs text-zinc-500">
            <span>
              {done
                ? 'Complete'
                : error
                ? 'Stopped'
                : totalPages
                ? `${pagesFullyDone} / ${totalPages} pages done`
                : `${overallPct}%`}
            </span>
            <span className="tabular-nums">{fmt(tick > 0 || done ? totalElapsed : 0)}</span>
          </div>
          <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-700 ease-out ${
                done ? 'bg-green-500' : error ? 'bg-red-600' : 'bg-[var(--accent)]'
              }`}
              style={{ width: `${overallPct}%` }}
            />
          </div>
          {/* Parallel pipeline indicator */}
          {runningStages.length > 1 && (
            <p className="text-xs text-blue-400/70">
              ⚡ {runningStages.length} stages running in parallel
            </p>
          )}
        </div>
      </div>

      {/* Parallel pipeline strip — compact overview of the 5 per-page stages */}
      {pipelineActive && (
        <div className="w-full max-w-2xl mb-4 animate-fade-in">
          <div className="card px-4 py-3">
            <p className="text-xs font-semibold text-zinc-600 uppercase tracking-wide mb-2.5">
              Per-page pipeline
            </p>
            <div className="flex gap-1.5">
              {PIPELINE_STAGES.map(key => {
                const s = stages[key]
                const progress = pct(s.page, s.total)
                const isActive = s.status === 'running' || s.status === 'done'
                return (
                  <div key={key} className="flex-1 min-w-0">
                    <div className={`rounded-lg px-2 py-2 border transition-all duration-300 ${
                      s.status === 'done'    ? 'bg-green-950/30 border-green-800/50' :
                      s.status === 'running' ? 'bg-blue-950/40 border-blue-700/60' :
                      s.status === 'error'   ? 'bg-red-950/30 border-red-800/50' :
                                               'bg-zinc-900/40 border-zinc-800/40'
                    }`}>
                      {/* Status icon + page count */}
                      <div className="flex items-center justify-between mb-1">
                        <span className={`text-sm leading-none ${
                          s.status === 'done'    ? 'text-green-400' :
                          s.status === 'running' ? 'text-blue-300' :
                          s.status === 'error'   ? 'text-red-400'  : 'text-zinc-600'
                        }`}>
                          {s.status === 'done'    ? '✓' :
                           s.status === 'running' ? '⟳' :
                           s.status === 'error'   ? '✗' :
                           STAGE_INFO[key].icon}
                        </span>
                        {s.status === 'running' && s.page != null && (
                          <span className="text-xs text-blue-400/80 tabular-nums font-mono">
                            {s.page}
                          </span>
                        )}
                        {s.status === 'done' && s.total != null && (
                          <span className="text-xs text-green-600/80 tabular-nums font-mono">
                            {s.total}
                          </span>
                        )}
                      </div>

                      {/* Stage name */}
                      <p className={`text-xs truncate ${
                        s.status === 'done'    ? 'text-zinc-500' :
                        s.status === 'running' ? 'text-zinc-400' : 'text-zinc-600'
                      }`}>
                        {STAGE_INFO[key].label}
                      </p>

                      {/* Mini progress bar */}
                      {isActive && (
                        <div className="mt-1.5 h-1 bg-zinc-800 rounded-full overflow-hidden">
                          {s.status === 'done' || (s.page != null && s.total != null) ? (
                            <div
                              className={`h-full rounded-full transition-all duration-500 ${
                                s.status === 'done' ? 'bg-green-500' : 'bg-blue-500'
                              }`}
                              style={{ width: `${s.status === 'done' ? 100 : progress}%` }}
                            />
                          ) : (
                            <div className="h-full w-1/2 bg-blue-500/50 rounded-full animate-pulse" />
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Stage list */}
      <div className="w-full max-w-2xl card p-2 mb-4 animate-slide-in">
        {STAGE_ORDER.map((key, idx) => (
          <StageRow
            key={key}
            stageKey={key}
            state={stages[key]}
            isLast={idx === STAGE_ORDER.length - 1}
            onResume={handleResume}
            resuming={resuming}
          />
        ))}
      </div>

      {/* Error banner with resume */}
      {error && failedStage && (
        <div className="w-full max-w-2xl mb-4 animate-fade-in">
          <div className="px-5 py-4 bg-red-950/40 border border-red-800 rounded-2xl">
            <div className="flex items-start gap-3">
              <span className="text-red-400 text-lg shrink-0 mt-0.5">⚠</span>
              <div className="flex-1 min-w-0">
                <p className="text-red-400 text-sm font-medium">
                  Failed during <span className="font-bold">{STAGE_INFO[failedStage]?.label}</span>
                </p>
                <p className="text-red-300/70 text-xs font-mono mt-1 break-words leading-relaxed">
                  {error}
                </p>
              </div>
            </div>
            <div className="flex gap-3 mt-4">
              <button
                onClick={() => handleResume(failedStage)}
                disabled={resuming}
                className="btn-primary text-sm px-4 py-2 flex items-center gap-2"
              >
                {resuming ? <><Spinner /> Resuming…</> : <>↺ Retry from {STAGE_INFO[failedStage]?.label}</>}
              </button>
              <button onClick={() => router.push('/')} className="btn-ghost text-sm px-4 py-2">
                Start over
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Done panel — waiting for library_ready (spinner) */}
      {done && !libraryId && !libraryTimedOut && (
        <div className="w-full max-w-2xl mb-4 animate-fade-in">
          <div className="card p-5">
            <div className="flex items-center gap-3">
              <span className="text-3xl">🎉</span>
              <div className="flex-1">
                <p className="font-semibold text-zinc-100">Translation complete!</p>
                <p className="text-sm text-zinc-400 mt-0.5">Adding to library — opening reader in a moment…</p>
              </div>
              <Spinner />
            </div>
            {downloadUrl && (
              <a
                href={`${backendUrl}${downloadUrl}`}
                download
                className="mt-3 block text-center btn-ghost text-sm py-2"
              >
                ⬇ Download PDF while waiting
              </a>
            )}
          </div>
        </div>
      )}

      {/* Done panel — library registration timed out */}
      {done && !libraryId && libraryTimedOut && (
        <div className="w-full max-w-2xl mb-4 animate-fade-in">
          <div className="card p-5">
            <div className="flex items-start gap-3 mb-3">
              <span className="text-3xl">✅</span>
              <div className="flex-1">
                <p className="font-semibold text-zinc-100">Translation complete!</p>
                <p className="text-sm text-amber-400/80 mt-0.5">
                  Library registration timed out. You can download the PDF below or check the library shortly.
                </p>
              </div>
            </div>
            {downloadUrl && (
              <a
                href={`${backendUrl}${downloadUrl}`}
                download
                className="block text-center btn-primary text-sm py-2"
              >
                ⬇ Download translated PDF
              </a>
            )}
            <button
              onClick={() => router.push('/')}
              className="mt-2 block w-full text-center btn-ghost text-sm py-2"
            >
              Go to library
            </button>
          </div>
        </div>
      )}

      {/* Cost breakdown */}
      {done && downloadUrl && (geminiCost || Object.keys(modalSeconds).length > 0) && (() => {
        const totalModalSec = Object.values(modalSeconds).reduce((a, b) => a + b, 0)
        const modalUsd  = totalModalSec * MODAL_T4_USD_PER_SEC
        const geminiUsd = geminiCost?.usd ?? 0
        const totalUsd  = modalUsd + geminiUsd
        const totalIls  = totalUsd * ILS_PER_USD

        return (
          <div className="w-full max-w-2xl mb-4 animate-fade-in">
            <div className="card p-5">
              <div className="rounded-xl bg-zinc-800/50 border border-zinc-700/50 px-4 py-3">
                <p className="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">
                  Chapter cost
                </p>

                <div className="flex items-baseline gap-2 mb-3">
                  <span className="text-2xl font-bold text-zinc-100">
                    ₪{totalIls.toFixed(3)}
                  </span>
                  <span className="text-sm text-zinc-500">
                    / ${totalUsd.toFixed(4)} USD
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-2 mb-3">
                  {/* Modal */}
                  <div className="bg-zinc-900/60 rounded-lg px-3 py-2">
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-xs text-zinc-400 font-medium">Modal GPU</p>
                      <p className="text-xs text-zinc-300 font-mono tabular-nums">
                        ${modalUsd.toFixed(4)}
                      </p>
                    </div>
                    <p className="text-xs text-zinc-600">
                      {totalModalSec.toFixed(0)}s wall-clock · T4 GPU
                    </p>
                    {Object.keys(modalSeconds).length > 0 && (
                      <div className="mt-1.5 space-y-0.5">
                        {(['detect', 'ocr', 'inpaint'] as const).map(s =>
                          modalSeconds[s] != null ? (
                            <div key={s} className="flex justify-between text-xs text-zinc-600">
                              <span className="capitalize">{s}</span>
                              <span className="font-mono">{modalSeconds[s].toFixed(0)}s</span>
                            </div>
                          ) : null
                        )}
                      </div>
                    )}
                  </div>

                  {/* Gemini */}
                  <div className="bg-zinc-900/60 rounded-lg px-3 py-2">
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-xs text-zinc-400 font-medium">Gemini API</p>
                      <p className="text-xs text-zinc-300 font-mono tabular-nums">
                        ${geminiUsd.toFixed(4)}
                      </p>
                    </div>
                    <p className="text-xs text-zinc-600">
                      {geminiCost ? `${geminiCost.tokens.total.toLocaleString()} tokens` : '—'}
                    </p>
                    {geminiCost && (
                      <div className="mt-1.5 space-y-0.5">
                        <div className="flex justify-between text-xs text-zinc-600">
                          <span>Input</span>
                          <span className="font-mono">{geminiCost.tokens.input.toLocaleString()}</span>
                        </div>
                        <div className="flex justify-between text-xs text-zinc-600">
                          <span>Output</span>
                          <span className="font-mono">{geminiCost.tokens.output.toLocaleString()}</span>
                        </div>
                        {geminiCost.tokens.think > 0 && (
                          <div className="flex justify-between text-xs text-zinc-600">
                            <span>Thinking</span>
                            <span className="font-mono">{geminiCost.tokens.think.toLocaleString()}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>

                <p className="text-xs text-zinc-600 text-center">
                  Modal T4 ~$1.00/hr (GPU+mem) · Gemini 2.5 Flash · ~₪{ILS_PER_USD}/USD
                </p>
              </div>
            </div>
          </div>
        )
      })()}

      {/* Activity log */}
      {activity.length > 0 && (
        <div className="w-full max-w-2xl animate-fade-in">
          <div className="card p-4">
            <p className="text-xs font-semibold text-zinc-500 uppercase tracking-wide mb-3">Activity</p>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {[...activity].reverse().map((entry, i) => (
                <div key={i} className="flex items-baseline gap-3 text-xs">
                  <span className="text-zinc-700 tabular-nums shrink-0 font-mono">
                    {new Date(entry.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                  </span>
                  <span className={`shrink-0 w-16 font-medium ${
                    entry.stage === 'error' ? 'text-red-500' :
                    entry.stage === 'done'  ? 'text-green-500' : 'text-zinc-500'
                  }`}>
                    {entry.stage === 'error' || entry.stage === 'done' ? '' : entry.stage}
                  </span>
                  <span className="text-zinc-400 leading-relaxed">{entry.text}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <p className="mt-8 text-xs text-zinc-700 text-center">
        Powered by comic-text-detector · Gemini Vision · LaMa · Gemini 2.5 Flash · python-bidi
      </p>
    </main>
  )
}

// ── StageRow ──────────────────────────────────────────────────────────────────

function StageRow({
  stageKey, state, isLast, onResume, resuming,
}: {
  stageKey: string
  state: StageState
  isLast: boolean
  onResume: (step: string) => void
  resuming: boolean
}) {
  const info = STAGE_INFO[stageKey]
  const { status, page, total, message, startedAt, finishedAt } = state

  // Per-stage elapsed timer
  const [stageElapsed, setStageElapsed] = useState(0)
  useEffect(() => {
    if (status !== 'running') { setStageElapsed(0); return }
    const start = startedAt ?? Date.now()
    const iv = setInterval(() => setStageElapsed(Date.now() - start), 500)
    return () => clearInterval(iv)
  }, [status, startedAt])

  const duration = finishedAt && startedAt ? finishedAt - startedAt : null
  const progress = pct(page, total)

  const containerClass = [
    'flex items-start gap-3 px-3 py-3 rounded-xl transition-all duration-300',
    status === 'running' ? 'bg-blue-950/30' :
    status === 'done'    ? 'bg-green-950/10' :
    status === 'error'   ? 'bg-red-950/20' : '',
    !isLast ? 'mb-0.5' : '',
  ].join(' ')

  const iconClass = [
    'shrink-0 w-9 h-9 rounded-full border-2 flex items-center justify-center text-sm font-bold transition-all duration-300',
    status === 'done'    ? 'border-green-600 bg-green-900/50 text-green-400' :
    status === 'running' ? 'border-blue-500 bg-blue-900/50 text-blue-300' :
    status === 'error'   ? 'border-red-600 bg-red-900/50 text-red-400' :
                           'border-zinc-700 bg-zinc-800/50 text-zinc-600',
  ].join(' ')

  return (
    <div className={containerClass}>
      {/* Icon */}
      <div className={iconClass}>
        {status === 'done'    ? '✓' :
         status === 'error'   ? '✗' :
         status === 'running' ? <Spinner /> :
         info.icon}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 pt-1">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <span className={`text-sm font-semibold transition-colors duration-300 ${
            status === 'done'    ? 'text-zinc-200' :
            status === 'running' ? 'text-white' :
            status === 'error'   ? 'text-red-400' : 'text-zinc-500'
          }`}>
            {info.label}
          </span>

          <div className="flex items-center gap-2 shrink-0">
            {/* Page counter */}
            {(status === 'running' || status === 'done') && page != null && total != null && (
              <span className={`text-xs tabular-nums font-mono ${
                status === 'done' ? 'text-green-600' : 'text-blue-400'
              }`}>
                {status === 'done' ? `${total} pages` : `${page} / ${total}`}
              </span>
            )}
            {/* Duration */}
            {status === 'running' && (
              <span className="text-xs text-zinc-600 tabular-nums">{fmt(stageElapsed)}</span>
            )}
            {status === 'done' && duration != null && (
              <span className="text-xs text-zinc-600 tabular-nums">{fmt(duration)}</span>
            )}
          </div>
        </div>

        {/* Description / status text */}
        <p className={`text-xs mt-0.5 transition-colors duration-300 ${
          status === 'running' ? 'text-blue-400/80' :
          status === 'done'    ? 'text-zinc-600' :
          status === 'error'   ? 'text-red-400/80' : 'text-zinc-700'
        }`}>
          {status === 'error'   ? (message ?? info.desc) :
           status === 'running' ? info.verb :
                                  info.desc}
        </p>

        {/* Progress bar */}
        {status === 'running' && total != null && total > 0 && (
          <div className="mt-2 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}
        {/* Indeterminate bar when no page count yet */}
        {status === 'running' && (page == null || total == null) && (
          <div className="mt-2 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div className="h-full w-1/3 bg-blue-500/60 rounded-full animate-pulse" />
          </div>
        )}

        {/* Per-stage resume button */}
        {status === 'error' && (
          <button
            onClick={() => onResume(stageKey)}
            disabled={resuming}
            className="mt-2 text-xs px-3 py-1.5 rounded-lg border border-red-700/60 text-red-400 hover:bg-red-950/40 transition-colors flex items-center gap-1.5"
          >
            {resuming ? <><Spinner /> Resuming…</> : <>↺ Retry from here</>}
          </button>
        )}
      </div>
    </div>
  )
}

// ── Spinner ───────────────────────────────────────────────────────────────────

function Spinner() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}
