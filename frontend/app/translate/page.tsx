'use client'

/**
 * Power-user upload page — paste a MangaDex URL or upload a PDF/ZIP directly.
 * This was the original homepage; now lives at /translate.
 */

import { useState, useRef, useCallback, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import Spinner from '@/components/Spinner'

// ── URL helpers ───────────────────────────────────────────────────────────────

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MANGADEX_CHAPTER_RE =
  /mangadex\.org\/chapter\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i

function extractUUID(raw: string): string | null {
  const s = raw.trim()
  if (UUID_RE.test(s)) return s
  const m = s.match(MANGADEX_CHAPTER_RE)
  return m ? m[1] : null
}

interface ChapterPreview {
  mangaTitle:   string
  chapterNum:   string | null
  chapterTitle: string | null
  language:     string
}

async function fetchChapterPreview(uuid: string): Promise<ChapterPreview> {
  const res = await fetch(
    `https://api.mangadex.org/chapter/${uuid}?includes[]=manga`,
    { signal: AbortSignal.timeout(6000) },
  )
  if (!res.ok) throw new Error('Chapter not found')
  const json = await res.json()
  const attrs    = json.data?.attributes ?? {}
  const mangaRel = (json.data?.relationships ?? []).find((r: { type: string }) => r.type === 'manga')
  const titles: Record<string, string> = mangaRel?.attributes?.title ?? {}
  const mangaTitle =
    titles['en'] ?? titles['ja-ro'] ?? titles['ja'] ?? Object.values(titles)[0] ?? 'Unknown manga'
  return {
    mangaTitle,
    chapterNum:   attrs.chapter ?? null,
    chapterTitle: attrs.title   ?? null,
    language:     attrs.translatedLanguage ?? '??',
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function TranslatePage() {
  const router = useRouter()
  const [tab, setTab] = useState<'url' | 'file'>('url')

  const [url,            setUrl]            = useState('')
  const [dataSaver,      setDataSaver]      = useState(false)
  const [urlValid,       setUrlValid]       = useState<boolean | null>(null)
  const [preview,        setPreview]        = useState<ChapterPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError,   setPreviewError]   = useState<string | null>(null)
  const previewAbortRef  = useRef<AbortController | null>(null)
  const previewTimerRef  = useRef<ReturnType<typeof setTimeout> | null>(null)

  const [file,    setFile]    = useState<File | null>(null)
  const [dragging,setDragging]= useState(false)

  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // ── URL preview ───────────────────────────────────────────────────────────

  useEffect(() => {
    const raw  = url.trim()
    if (previewTimerRef.current) clearTimeout(previewTimerRef.current)
    previewAbortRef.current?.abort()
    if (!raw) { setUrlValid(null); setPreview(null); setPreviewError(null); return }
    const uuid = extractUUID(raw)
    if (!uuid) { setUrlValid(false); setPreview(null); setPreviewError(null); return }
    setUrlValid(true); setPreview(null); setPreviewError(null); setPreviewLoading(true)
    previewTimerRef.current = setTimeout(async () => {
      const ctrl = new AbortController()
      previewAbortRef.current = ctrl
      try {
        const info = await fetchChapterPreview(uuid)
        if (!ctrl.signal.aborted) { setPreview(info); setPreviewLoading(false) }
      } catch {
        if (!ctrl.signal.aborted) { setPreviewError('Could not fetch chapter info — it will still translate.'); setPreviewLoading(false) }
      }
    }, 500)
    return () => { if (previewTimerRef.current) clearTimeout(previewTimerRef.current); previewAbortRef.current?.abort() }
  }, [url])

  // ── Submit ────────────────────────────────────────────────────────────────

  const submitUrl = async () => {
    const raw = url.trim()
    if (!raw) { setError('Please enter a MangaDex chapter URL.'); return }
    if (!extractUUID(raw)) { setError("This doesn't look like a MangaDex chapter URL."); return }
    setLoading(true); setError('')
    try {
      const res  = await fetch('/api/jobs/from-url', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: raw, data_saver: dataSaver }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to start job.')
      if (data.cached && data.library_id) router.push(`/library/${data.library_id}`)
      else router.push(`/jobs/${data.job_id}`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
      setLoading(false)
    }
  }

  const submitFile = async () => {
    if (!file) { setError('Please select a file.'); return }
    setLoading(true); setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      const res  = await fetch('/api/jobs', { method: 'POST', body: form })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to start job.')
      router.push(`/jobs/${data.job_id}`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Something went wrong.')
      setLoading(false)
    }
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); setDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) {
      const ext = f.name.split('.').pop()?.toLowerCase()
      if (ext === 'pdf' || ext === 'zip') { setFile(f); setError('') }
      else setError('Only .pdf and .zip files are supported.')
    }
  }, [])

  const inputBorderClass =
    urlValid === false ? 'border-red-600 focus:ring-red-500' :
    urlValid === true  ? 'border-green-700 focus:ring-green-500' : ''

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-[calc(100vh-3.5rem)] flex flex-col items-center justify-center px-4 py-12">

      <div className="text-center mb-10">
        <h1 className="text-3xl font-bold text-zinc-50 mb-2">Translate a Chapter</h1>
        <p className="text-zinc-400">Paste a MangaDex link or upload a file — we handle the rest.</p>
      </div>

      <div className="card w-full max-w-xl p-8">

        {/* Tabs */}
        <div className="flex gap-1 bg-zinc-800 p-1 rounded-xl mb-8">
          {(['url', 'file'] as const).map(t => (
            <button key={t} onClick={() => { setTab(t); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                tab === t ? 'bg-zinc-700 text-zinc-100 shadow-sm' : 'text-zinc-400 hover:text-zinc-200'
              }`}>
              {t === 'url' ? '🔗 MangaDex URL' : '📁 Upload File'}
            </button>
          ))}
        </div>

        {/* URL tab */}
        {tab === 'url' && (
          <div className="space-y-4">
            <div className="relative flex items-center gap-2">
              <div className="relative flex-1">
                <input className={`input pr-8 ${inputBorderClass}`} type="text"
                  placeholder="https://mangadex.org/chapter/…" value={url}
                  onChange={e => { setUrl(e.target.value); setError('') }}
                  onKeyDown={e => e.key === 'Enter' && !loading && submitUrl()}
                  spellCheck={false} />
                {urlValid === true  && <span className="absolute right-3 top-1/2 -translate-y-1/2 text-green-500 text-sm">✓</span>}
                {urlValid === false && <span className="absolute right-3 top-1/2 -translate-y-1/2 text-red-500 text-sm">✗</span>}
              </div>
              <button onClick={async () => { try { setUrl(await navigator.clipboard.readText()) } catch {} }}
                className="shrink-0 px-3 py-3 rounded-xl border border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 text-xs transition-all"
                title="Paste">📋</button>
            </div>

            {urlValid === true && (
              <div className="rounded-xl border border-zinc-700 bg-zinc-800/50 px-4 py-3">
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
                    <div className="w-8 h-8 rounded-lg bg-blue-900/60 border border-blue-800 flex items-center justify-center text-base shrink-0">📚</div>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-zinc-100 truncate">{preview.mangaTitle}</p>
                      <p className="text-xs text-zinc-500 mt-0.5">
                        {preview.chapterNum != null ? `Chapter ${preview.chapterNum}` : 'Oneshot'}
                        {preview.chapterTitle ? ` · ${preview.chapterTitle}` : ''}
                      </p>
                    </div>
                    <span className="ml-auto shrink-0 text-green-500 text-sm">✓</span>
                  </div>
                ) : null}
              </div>
            )}

            <label className="flex items-center gap-3 cursor-pointer select-none group">
              <div className={`relative w-10 h-6 rounded-full transition-colors ${dataSaver ? 'bg-blue-600' : 'bg-zinc-700'}`}
                onClick={() => setDataSaver(v => !v)}>
                <div className={`absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-transform ${dataSaver ? 'translate-x-5' : 'translate-x-1'}`} />
              </div>
              <span className="text-sm text-zinc-400">Data saver mode <span className="text-zinc-600">(faster)</span></span>
            </label>
          </div>
        )}

        {/* File tab */}
        {tab === 'file' && (
          <div
            className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-all ${
              dragging ? 'border-blue-500 bg-blue-950/20' :
              file     ? 'border-green-600 bg-green-950/20' :
                         'border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800/50'
            }`}
            onDragOver={e => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => fileRef.current?.click()}>
            <input ref={fileRef} type="file" accept=".pdf,.zip" className="hidden"
              onChange={e => { const f = e.target.files?.[0]; if (f) { setFile(f); setError('') } }} />
            {file ? (
              <div className="space-y-2">
                <div className="text-3xl">{file.name.endsWith('.pdf') ? '📄' : '🗜️'}</div>
                <p className="font-medium text-zinc-200">{file.name}</p>
                <p className="text-sm text-zinc-500">{(file.size / 1024 / 1024).toFixed(1)} MB · Click to change</p>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="text-3xl text-zinc-600">📂</div>
                <p className="font-medium text-zinc-300">Drop your file here</p>
                <p className="text-sm text-zinc-600">.pdf or .zip · max 200 MB</p>
              </div>
            )}
          </div>
        )}

        {error && (
          <div className="mt-4 px-4 py-3 bg-red-950/50 border border-red-800 rounded-xl text-red-400 text-sm flex gap-2">
            <span className="shrink-0">⚠</span> {error}
          </div>
        )}

        <button
          className="btn-primary w-full mt-6 flex items-center justify-center gap-2"
          onClick={tab === 'url' ? submitUrl : submitFile}
          disabled={loading || (tab === 'url' && urlValid === false)}>
          {loading ? <><Spinner size="sm" /> Starting…</> : <>Translate to Hebrew →</>}
        </button>
      </div>
    </main>
  )
}
