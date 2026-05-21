/**
 * SSE proxy — NOT used in development.
 *
 * The browser connects directly to FastAPI via NEXT_PUBLIC_BACKEND_URL
 * (defaults to http://localhost:8000) because Node.js's built-in fetch
 * (undici) buffers the entire response body before streaming it, making
 * SSE unusable through any Next.js route handler that uses fetch().
 *
 * This file exists so that in production, if you point NEXT_PUBLIC_BACKEND_URL
 * to a reverse proxy (nginx with proxy_buffering off), SSE works correctly
 * without exposing the internal backend URL to the browser.
 *
 * Production nginx config snippet for SSE:
 *   location /api/jobs/ {
 *     proxy_pass         http://backend:8000;
 *     proxy_buffering    off;
 *     proxy_cache        off;
 *     proxy_read_timeout 3600s;
 *     add_header         X-Accel-Buffering no;
 *   }
 */

export const dynamic = 'force-dynamic'

// This route intentionally has no handler — traffic goes direct to backend.
