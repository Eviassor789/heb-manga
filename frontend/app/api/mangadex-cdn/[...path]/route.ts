/**
 * Proxy for MangaDex cover images.
 *
 * uploads.mangadex.org uses hotlink protection — it returns an HTML page
 * ("you can watch this on mangadex.com") when the Referer is not mangadex.org.
 * This route fetches covers server-side with the correct Referer so the phone
 * (or any device) never hits the CDN directly.
 *
 * /api/mangadex-cdn/<path>  →  https://uploads.mangadex.org/<path>
 */

export async function GET(
  _request: Request,
  { params }: { params: { path: string[] } },
) {
  const imgPath = (params.path ?? []).join('/')
  const url     = `https://uploads.mangadex.org/${imgPath}`

  let res: Response
  try {
    res = await fetch(url, {
      // Only send Referer — anything extra (Origin, fake UA) can confuse the CDN
      headers: { Referer: 'https://mangadex.org' },
    })
  } catch {
    return new Response(null, { status: 502 })
  }

  if (!res.ok) return new Response(null, { status: res.status })

  return new Response(res.body, {
    headers: {
      'Content-Type':  res.headers.get('content-type') ?? 'image/jpeg',
      'Cache-Control': 'public, max-age=86400, stale-while-revalidate=3600',
    },
  })
}
