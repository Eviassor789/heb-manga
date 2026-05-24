'use client'

import { useParams } from 'next/navigation'
import SeriesDetailPage from '@/components/SeriesDetailPage'

export default function MangaPage() {
  const { id } = useParams<{ id: string }>()
  return <SeriesDetailPage id={id} source="mangadex" />
}
