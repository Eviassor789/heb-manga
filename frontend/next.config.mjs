// Backend base URL used for the server-side rewrite fallback below. Falls
// back to the local FastAPI dev server. In production this is the deployed
// backend's HTTPS URL — same value as NEXT_PUBLIC_BACKEND_URL (set both on
// the hosting platform).
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000'

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Allow the Next.js dev server to serve requests that originate from other
  // devices on your local network (e.g. your phone on the same WiFi).
  // The '*' wildcard covers any local IP so you don't have to hard-code yours.
  allowedDevOrigins: ['*'],

  async rewrites() {
    return {
      // afterFiles: checked after filesystem routes/pages but BEFORE dynamic
      // file-based routes. There's no filesystem route at /api/mangadex/*, so
      // this always applies — proxies MangaDex API through Next.js so mobile
      // devices don't need a direct connection to api.mangadex.org (avoids
      // CORS/ISP issues on phones).
      afterFiles: [
        {
          source: '/api/mangadex/:path*',
          destination: 'https://api.mangadex.org/:path*',
        },
      ],
      // fallback: only applied if NO filesystem route matched, including
      // dynamic API routes. This lets app/api/mangadex-cdn/[...path]/route.ts
      // (which adds the Referer header required by uploads.mangadex.org's
      // hotlink protection) handle its own requests, while every other
      // /api/* path falls through to the FastAPI backend.
      fallback: [
        {
          source: '/api/:path*',
          destination: `${BACKEND_URL}/api/:path*`,
        },
      ],
    }
  },
}

export default nextConfig
