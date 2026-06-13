# Deployment Guide — Going Live (Multi-User)

This guide takes the app from "runs on my PC" to "anyone on the internet can use
it." Read the **architecture** section first — it explains the one decision that
trips everyone up.

---

## 1. Architecture: two hosts, not one

> ⚠️ **The backend cannot run on Vercel.** Vercel hosts serverless functions,
> which (a) are killed when they return a response — so the multi-minute
> translation pipeline and the background janitor task would die mid-run,
> (b) have a hard execution-time cap, and (c) can't hold the long-lived SSE
> connection that streams job progress. The backend also keeps job state,
> caches, and the per-IP/concurrency guards **in process memory**, which
> requires a single always-on process. Serverless gives you many short-lived
> ones instead.

So the app deploys as **two pieces**:

| Piece | Host | Why |
|-------|------|-----|
| **Frontend** (Next.js) | **Vercel** ✅ (free tier is perfect) | Static/SSR Next.js is exactly what Vercel is for. |
| **Backend** (FastAPI) | **A container host** — Render / Railway / Fly.io | Needs a long-lived process, background tasks, SSE, and local working disk. |

The browser talks to the frontend (Vercel) for pages, and **directly** to the
backend host for API calls + SSE. They are wired together by one env var:
`NEXT_PUBLIC_BACKEND_URL`.

```
 Browser ──► Vercel (Next.js frontend)
    │
    └──────► https://your-backend-host  (FastAPI: API + SSE)
                   │
                   ├──► Supabase  (chapter library metadata)
                   ├──► Cloudflare R2  (page images + PDFs)
                   └──► each user's Gemini + Modal accounts (BYOK)
```

---

## 2. What was hardened for production (already in the code)

These are implemented — you only need to configure env vars:

- **BYOK enforced** — every job must supply its own Gemini key **and** Modal
  tokens. Detection/inpainting always run on the user's Modal GPU; there is no
  server-side GPU fallback and no `USE_MODAL` off-switch. Your server never pays
  for translation or GPU.
- **Concurrency cap** — `MAX_CONCURRENT_JOBS` (default 2) pipelines run heavy
  stages at once; extra jobs queue for a slot.
- **One active job per IP** — a visitor can't queue dozens of jobs and starve
  the queue (HTTP 429 if they try).
- **Rate limiting** — WeebCentral scrape/search endpoints: `SCRAPE_RATE_PER_MIN`
  (default 30/min/IP); library reads: `API_RATE_PER_MIN` (default 120/min/IP).
  Protects your server's IP from being throttled/blocked upstream.
- **Response caching** — WeebCentral featured/search/series/chapters are cached
  in memory for a few minutes. MangaDex covers are cached at the CDN/browser via
  the `/api/mangadex-cdn` proxy's `Cache-Control` headers, and MangaDex search
  is already client-cached in the browser.
- **CORS lockdown** — set `ALLOWED_ORIGINS` to your exact frontend domain(s).
- **Disk hygiene** — incomplete/abandoned job folders are removed on startup and
  by a background janitor every 3h (`JANITOR_*`).
- **Health check** — `GET /health` (and `/healthz`) for the platform's probe.

---

## 3. One-time cloud setup (FULL CLOUD storage)

### 3a. Supabase (chapter library DB)
1. Create a free project at https://supabase.com.
2. SQL editor → run the `chapters` table DDL documented at the top of
   `backend/core/library.py` (table + RLS + `public_read` policy).
3. Settings → API → copy the **Project URL** (`SUPABASE_URL`) and the
   **`service_role`** key (`SUPABASE_KEY`). The service_role key is a secret —
   backend only, never in the frontend.

### 3b. Cloudflare R2 (page images + PDFs)
1. Cloudflare dashboard → R2 → create bucket `manga-chapters`.
2. Enable **Public Access** on the bucket; note its public URL → `R2_PUBLIC_URL`.
3. R2 → Manage API Tokens → create a token → `R2_ACCOUNT_ID`,
   `R2_ACCESS_KEY_ID`, `R2_SECRET_KEY`.

The backend auto-detects FULL CLOUD mode once all 7 vars are present
(verify later via `GET /health` → `"library_mode": "cloud"`).

---

## 4. Deploy the backend (Render example)

A ready blueprint lives at `render.yaml`.

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. It reads `render.yaml` and
   creates a Docker web service from `backend/Dockerfile`.
3. Fill in the secret env vars (the `sync: false` ones): `ALLOWED_ORIGINS`
   (set after step 5 once you know the Vercel URL), `SUPABASE_URL`,
   `SUPABASE_KEY`, and all `R2_*`.
4. Deploy. First build is slow (installs CPU torch + warms EasyOCR). When live,
   hit `https://<service>.onrender.com/health` — expect `{"status":"ok", ...}`.

> **Free-tier caveat:** The blueprint uses Render's **free** web service. It
> sleeps after ~15 min idle (the next request takes ~30-60s to cold-start) and
> has 512 MB RAM, which is tight once EasyOCR/torch load — watch the logs after
> the first OCR fallback call for an OOM/restart. If that happens, change
> `plan: free` to `plan: starter` (~$7/mo, 512MB → more headroom, no sleep) in
> `render.yaml` and redeploy. **Railway** and **Fly.io** are equivalent
> alternatives if you'd rather not use Render at all — all use the same
> `backend/Dockerfile`:
> - **Railway:** New Project → Deploy from repo → it detects the Dockerfile.
>   Set the same env vars. Set the service root to `backend/` if asked.
> - **Fly.io:** `cd backend && fly launch --dockerfile Dockerfile` then
>   `fly secrets set SUPABASE_URL=... R2_SECRET_KEY=... ...`.

The backend host gives you **HTTPS automatically** — note that URL; it's what
the frontend will point at.

---

## 5. Deploy the frontend (Vercel)

1. Vercel → **New Project** → import the repo.
2. Set **Root Directory = `frontend`** (so Vercel builds the Next.js app).
3. Add env var **`NEXT_PUBLIC_BACKEND_URL`** = your backend's HTTPS URL from
   step 4 (e.g. `https://hmt-backend.onrender.com`). No trailing slash.
4. Deploy. Vercel gives you `https://your-app.vercel.app`.
5. **Go back to the backend host** and set `ALLOWED_ORIGINS` to that exact
   Vercel URL (plus any custom domain), then redeploy/restart the backend.

---

## 6. TLS + SSE — what to confirm

Server-Sent Events (job progress) break if anything buffers the stream.

- **HTTPS is mandatory.** The frontend is HTTPS, so the backend must be too, or
  browsers block the mixed-content API/SSE calls. Render/Railway/Fly all
  terminate TLS for you — nothing to configure.
- **No buffering proxy in front.** The managed hosts above don't buffer by
  default. The app already sends `Cache-Control: no-cache` and
  `X-Accel-Buffering: no` on the SSE response, and uvicorn runs with
  `--timeout-keep-alive 75` so long-lived streams aren't dropped early.
- **If you put your own Nginx/Caddy in front** (only on a raw VPS), disable
  buffering for the API location:
  ```nginx
  location /api/ {
      proxy_pass http://127.0.0.1:8000;
      proxy_buffering off;
      proxy_read_timeout 3600s;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
  ```
  The `X-Forwarded-For` header matters — the rate limiter and per-IP job limit
  use it to identify the real client.

---

## 7. Secrets checklist

- `.env` is gitignored (verified). **Never commit real keys.** Set them in the
  host's env/secrets manager instead (Render/Railway/Fly dashboards, Vercel
  project env).
- On the **public server, leave `GEMINI_API_KEY` blank** — translation is BYOK,
  so the server should have no key of its own to accidentally bill.
- `SUPABASE_KEY` is the **service_role** key — backend only. It must never reach
  the browser. The frontend only ever gets `NEXT_PUBLIC_*` vars.
- If any real key was ever committed in the past, **rotate it** (new key, revoke
  old) — git history keeps old values.
- Only `NEXT_PUBLIC_BACKEND_URL` is exposed to the browser; that's fine, it's a
  public URL.

---

## 8. Tunable env vars (all optional, sensible defaults)

| Var | Default | Purpose |
|-----|---------|---------|
| `ALLOWED_ORIGINS` | `*` | Comma-separated exact frontend origins (CORS). |
| `MAX_CONCURRENT_JOBS` | `2` | Pipelines running heavy stages at once. |
| `SCRAPE_RATE_PER_MIN` | `30` | Per-IP/min limit on WeebCentral endpoints. |
| `API_RATE_PER_MIN` | `120` | Per-IP/min limit on library read endpoints. |
| `JANITOR_INTERVAL_SECONDS` | `10800` | Janitor sweep interval (3h). |
| `JANITOR_GRACE_SECONDS` | `3600` | Min idle age before a stuck job is deleted. |
| `MAX_PAGES_PER_JOB` | `50` | Page cap per chapter. |

> **Scaling note:** the backend is designed as a **single instance**. Job state,
> SSE subscribers, the response cache, rate-limit counters, and the per-IP job
> map all live in process memory. To run multiple instances later, move those to
> Redis (cache + rate limit + job registry) — until then, run exactly one.
