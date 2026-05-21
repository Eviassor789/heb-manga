'use client'

import { useState, useRef, useCallback, useEffect } from 'react'
import { useRouter } from 'next/navigation'

// ── URL validation helpers ────────────────────────────────────────────────────

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MANGADEX_CHAPTER_RE =
  /mangadex\.org\/chapter\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i

function extractUUID(raw: string): string | null {
  const s = raw.trim()
  if (UUID_RE.test(s)) return s
  const m = s.match(MANGADEX_CHAPTER_RE)
  return m ? m[1] : null
}

function isMangaDexLike(raw: string): boolean {
  const s = raw.trim()
  if (!s) return true // empty is fine, don't show error yet
  return extractUUID(s) !== null
}

// ── Chapter preview type ──────────────────────────────────────────────────────

interface ChapterPreview {
  mangaTitle: string
  chapterNum: string | null
  chapterTitle: string | null
  language: string
}

async function fetchChapterPreview(uuid: string): Promise<ChapterPreview> {
  const res = await fetch(
    `https://api.mangadex.org/chapter/${uuid}?includes[]=manga`,
    { signal: AbortSignal.timeout(6000) },
  )
  if (!res.ok) throw new Error('Chapter not found')
  const json = await res.json()
  const attrs = json.data?.attributes ?? {}
  const mangaRel = (json.data?.relationships ?? []).find((r: { type: string }) => r.type === 'manga')
  const titles: Record<string, string> = mangaRel?.attributes?.title ?? {}
  const mangaTitle =
    titles['en'] ?? titles['ja-ro'] ?? titles['ja'] ?? Object.values(titles)[0] ?? 'Unknown manga'

  return {
    mangaTitle,
    chapterNum: attrs.chapter ?? null,
    chapterTitle: attrs.title ?? null,
    language: attrs.translatedLanguage ?? '??',
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function HomePage() {
  const router = useRouter()
  const [tab, setTab] = useState<'url' | 'file'>('url')

  // URL tab state
  const [url, setUrl] = useState('')
  const [dataSaver, setDataSaver] = useState(false)
  const [urlValid, setUrlValid] = useState<boolean | null>(null) // null = empty
  const [preview, setPreview] = useState<ChapterPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const previewAbortRef = useRef<AbortController | null>(null)
  const previewTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // File tab state
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)

  // Shared state
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // ── URL validation + preview (debounced) ───────────────────────────────────

  useEffect(() => {
    const raw = url.trim()

    // Clear old timer/abort
    if (previewTimerRef.current) clearTimeout(previewTimerRef.current)
    previewAbortRef.current?.abort()

    if (!raw) {
      setUrlValid(null)
      setPreview(null)
      setPreviewError(null)
      return
    }

    const uuid = extractUUID(raw)

    if (!uuid) {
      setUrlValid(false)
      setPreview(null)
      setPreviewError(null)
      return
    }

    setUrlValid(true)
    setPreview(null)
    setPreviewError(null)
    setPreviewLoading(true)

    previewTimerRef.current = setTimeout(async () => {
      const ctrl = new AbortController()
      previewAbortRef.current = ctrl
      try {
        const info = await fetchChapterPreview(uuid)
        if (!ctrl.signal.aborted) {
          setPreview(info)
          setPreviewLoading(false)
        }
      } catch {
        if (!ctrl.signal.aborted) {
          setPreviewError('Could not fetch chapter info — it will still translate fine.')
          setPreviewLoading(false)
        }
      }
    }, 500)

    return () => {
      if (previewTimerRef.current) clearTimeout(previewTimerRef.current)
      previewAbortRef.current?.abort()
    }
  }, [url])

  // ── Paste from clipboard ───────────────────────────────────────────────────

  const pasteFromClipboard = async () => {
    try {
      const text = await navigator.clipboard.readText()
      if (text) {
        setUrl(text)
        setError('')
      }
    } catch {
      // Clipboard not available — ignore silently
    }
  }

  // ── Drag & drop ────────────────────────────────────────────────────────────

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files[0]
    if (dropped) {
      const ext = dropped.name.split('.').pop()?.toLowerCase()
      if (ext === 'pdf' || ext === 'zip') {
        setFile(dropped)
        setError('')
      } else {
        setError('Only .pdf and .zip files are supported.')
      }
    }
  }, [])

  // ── Submit handlers ────────────────────────────────────────────────────────

  const submitUrl = async () => {
    const raw = url.trim()
    if (!raw) { setError('Please enter a MangaDex chapter URL.'); return }
    if (!extractUUID(raw)) { setError('This doesn\'t look like a MangaDex chapter URL or UUID.'); return }
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/api/jobs/from-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: raw, data_saver: dataSaver }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to start job.')
      router.push(`/jobs/${data.job_id}`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
      setLoading(false)
    }
  }

  const submitFile = async () => {
    if (!file) { setError('Please select a file.'); return }
    setLoading(true)
    setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch('/api/jobs', { method: 'POST', body: form })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to start job.')
      router.push(`/jobs/${data.job_id}`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
      setLoading(false)
    }
  }

  const handleSubmit = tab === 'url' ? submitUrl : submitFile

  // ── URL input border color ─────────────────────────────────────────────────

  const inputBorderClass =
    urlValid === false
      ? 'border-red-600 focus:ring-red-500'
      : urlValid === true
      ? 'border-green-700 focus:ring-green-500'
      : '' // default from .input class

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-4 py-16">

      {/* Header */}
      <div className="text-center mb-12 animate-fade-in">
        <div className="inline-flex items-center gap-3 mb-4">
          <span className="text-4xl">🈺</span>
          <h1 className="text-4xl font-bold tracking-tight text-zinc-50">
            Hebrew Manga Translator
          </h1>
        </div>
        <p className="text-zinc-400 text-lg max-w-md mx-auto">
          Paste a MangaDex link or upload a file — we handle the rest.
        </p>
      </div>

      {/* Main card */}
      <div className="card w-full max-w-xl p-8 animate-slide-in">

        {/* Tab switcher */}
        <div className="flex gap-1 bg-zinc-800 p-1 rounded-xl mb-8">
          {(['url', 'file'] as const).map((t) => (
            <button
              key={t}
              onClick={() => { setTab(t); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
                tab === t
                  ? 'bg-zinc-700 text-zinc-100 shadow-sm'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {t === 'url' ? '🔗 MangaDex URL' : '📁 Upload File'}
            </button>
          ))}
        </div>

        {/* ── URL tab ── */}
        {tab === 'url' && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-zinc-400 mb-2">
                Chapter URL or UUID
              </label>

              {/* Input row */}
              <div className="relative flex items-center gap-2">
                <div className="relative flex-1">
                  <input
                    className={`input pr-8 ${inputBorderClass}`}
                    type="text"
                    placeholder="https://mangadex.org/chapter/…"
                    value={url}
                    onChange={(e) => { setUrl(e.target.value); setError('') }}
                    onKeyDown={(e) => e.key === 'Enter' && !loading && handleSubmit()}
                    spellCheck={false}
                  />
                  {/* Validation icon inside input */}
                  {urlValid === true && (
                    <span className="absolute right-3 top-1/2 -translate-y-1/2 text-green-500 text-sm select-none">✓</span>
                  )}
                  {urlValid === false && (
                    <span className="absolute right-3 top-1/2 -translate-y-1/2 text-red-500 text-sm select-none">✗</span>
                  )}
                  {/* Clear button */}
                  {url && urlValid === null && (
                    <button
                      onClick={() => { setUrl(''); setError('') }}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-zinc-600 hover:text-zinc-400 text-sm transition-colors"
                      tabIndex={-1}
                    >
                      ✕
                    </button>
                  )}
                </div>

                {/* Paste button */}
                <button
                  onClick={pasteFromClipboard}
                  className="shrink-0 px-3 py-3 rounded-xl border border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 text-xs font-medium transition-all duration-150"
                  title="Paste from clipboard"
                >
                  📋
                </button>
              </div>

              {/* Validation hint */}
              {urlValid === false && (
                <p className="mt-1.5 text-xs text-red-400 flex items-center gap-1">
                  <span>Must be a MangaDex chapter URL or UUID</span>
                  <span className="text-zinc-600">·</span>
                  <a
                    href="https://mangadex.org"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-zinc-500 hover:text-zinc-300 underline underline-offset-2"
                  >
                    mangadex.org
                  </a>
                </p>
              )}
            </div>

            {/* Chapter preview card */}
            {urlValid === true && (
              <div className={`rounded-xl border px-4 py-3 transition-all duration-300 ${
                previewError
                  ? 'border-zinc-700 bg-zinc-800/30'
                  : 'border-zinc-700 bg-zinc-800/50'
              }`}>
                {previewLoading ? (
                  <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-lg bg-zinc-700 animate-pulse" />
                    <div className="space-y-1.5 flex-1">
                      <div className="h-3 bg-zinc-700 rounded animate-pulse w-2/3" />
                      <div className="h-2.5 bg-zinc-700 rounded animate-pulse w-1/3" />
                    </div>
                  </div>
                ) : previewError ? (
                  <p className="text-xs text-zinc-500">{previewError}</p>
                ) : preview ? (
                  <div className="flex items-start gap-3">
                    <div className="w-8 h-8 rounded-lg bg-blue-900/60 border border-blue-800 flex items-center justify-center text-base shrink-0">
                      📚
                    </div>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-zinc-100 truncate">{preview.mangaTitle}</p>
                      <p className="text-xs text-zinc-500 mt-0.5">
                        {preview.chapterNum != null ? `Chapter ${preview.chapterNum}` : 'Oneshot'}
                        {preview.chapterTitle ? ` · ${preview.chapterTitle}` : ''}
                        <span className="ml-2 uppercase tracking-wide text-zinc-600">{preview.language}</span>
                      </p>
                    </div>
                    <span className="ml-auto shrink-0 text-green-500 text-sm">✓</span>
                  </div>
                ) : null}
              </div>
            )}

            {/* Data saver toggle */}
            <label className="flex items-center gap-3 cursor-pointer select-none group">
              <div
                className={`relative w-10 h-6 rounded-full transition-colors duration-200 ${
                  dataSaver ? 'bg-blue-600' : 'bg-zinc-700'
                }`}
                onClick={() => setDataSaver((v) => !v)}
              >
                <div className={`absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-transform duration-200 ${
                  dataSaver ? 'translate-x-5' : 'translate-x-1'
                }`} />
              </div>
              <span className="text-sm text-zinc-400 group-hover:text-zinc-300 transition-colors">
                Data saver mode <span className="text-zinc-600">(lower resolution, faster)</span>
              </span>
            </label>
          </div>
        )}

        {/* ── File tab ── */}
        {tab === 'file' && (
          <div className="space-y-4">
            <div
              className={`border-2 border-dashed rounded-xl p-10 text-center transition-all duration-150 cursor-pointer ${
                dragging
                  ? 'border-blue-500 bg-blue-950/20'
                  : file
                  ? 'border-green-600 bg-green-950/20'
                  : 'border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800/50'
              }`}
              onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={() => fileRef.current?.click()}
            >
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,.zip"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) { setFile(f); setError('') }
                }}
              />
              {file ? (
                <div className="space-y-2">
                  <div className="text-3xl">
                    {file.name.endsWith('.pdf') ? '📄' : '🗜️'}
                  </div>
                  <p className="font-medium text-zinc-200">{file.name}</p>
                  <p className="text-sm text-zinc-500">
                    {(file.size / 1024 / 1024).toFixed(1)} MB
                    <span className="mx-1.5 text-zinc-700">·</span>
                    <span className="uppercase text-xs tracking-wide text-zinc-600 font-medium">
                      {file.name.split('.').pop()}
                    </span>
                    <span className="mx-1.5 text-zinc-700">·</span>
                    Click to change
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="text-3xl text-zinc-600">📂</div>
                  <p className="font-medium text-zinc-300">Drop your file here</p>
                  <p className="text-sm text-zinc-600">.pdf or .zip · max 200 MB</p>
                </div>
              )}
            </div>

            {/* Supported format hints */}
            <div className="flex items-center gap-3 text-xs text-zinc-600">
              <span className="flex items-center gap-1">
                <span className="text-sm">📄</span> PDF — single manga volume or chapter
              </span>
              <span className="text-zinc-800">·</span>
              <span className="flex items-center gap-1">
                <span className="text-sm">🗜️</span> ZIP — folder of page images
              </span>
            </div>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="mt-4 px-4 py-3 bg-red-950/50 border border-red-800 rounded-xl text-red-400 text-sm flex items-start gap-2">
            <span className="shrink-0 mt-0.5">⚠</span>
            <span>{error}</span>
          </div>
        )}

        {/* Submit */}
        <button
          className="btn-primary w-full mt-6 flex items-center justify-center gap-2"
          onClick={handleSubmit}
          disabled={loading || (tab === 'url' && urlValid === false)}
        >
          {loading ? (
            <>
              <Spinner />
              Starting job…
            </>
          ) : (
            <>
              Translate to Hebrew
              <span className="text-lg">→</span>
            </>
          )}
        </button>
      </div>

      {/* Footer note */}
      <p className="mt-8 text-xs text-zinc-600 text-center">
        Powered by comic-text-detector · EasyOCR · LaMa · Gemini · python-bidi
      </p>
    </main>
  )
}

function Spinner() {
  return (
    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}
