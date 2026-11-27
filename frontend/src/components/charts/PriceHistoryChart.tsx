"use client"

import { TickerChart } from "@/components/charts/TickerChart"

/** Kept for compatibility; the ticker page uses TickerChart directly. */
export function PriceHistoryChart({ data, loading }: { data: any[]; loading?: boolean }) {
    return <TickerChart history={[]} priceData={data} capData={[]} loading={loading} defaultSeries="price" />
}
