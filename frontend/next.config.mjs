/** @type {import('next').NextConfig} */
const nextConfig = {
  // Allow the Next.js dev server to serve requests that originate from other
  // devices on your local network (e.g. your phone on the same WiFi).
  // The '*' wildcard covers any local IP so you don't have to hard-code yours.
  allowedDevOrigins: ['*'],

  async rewrites() {
    return [
      // Proxy MangaDex API through Next.js server so mobile devices don't need
      // a direct connection to api.mangadex.org (avoids CORS/ISP issues on phones).
      // The cover CDN (/api/mangadex-cdn/*) is handled by a file-based API route
      // (app/api/mangadex-cdn/[...path]/route.ts) that adds the correct Referer
      // header required by uploads.mangadex.org hotlink protection.
      // Must come BEFORE the catch-all /api/:path* rule.
      {
        source: '/api/mangadex/:path*',
        destination: 'https://api.mangadex.org/:path*',
      },
      {
        source: '/api/:path*',
        destination: 'http://localhost:8000/api/:path*',
      },
    ]
  },
}

export default nextConfig
