'use client'

import { useParams } from 'next/navigation'
import SeriesDetailPage from '@/components/SeriesDetailPage'

export default function WeebCentralPage() {
  const { id } = useParams<{ id: string }>()
  return <SeriesDetailPage id={id} source="weebcentral" />
}
