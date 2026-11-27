"use client"

import { TickerChart } from "@/components/charts/TickerChart"

/** Kept for compatibility; the ticker page uses TickerChart directly. */
export function MarketCapChart({ data, loading }: { data: any[]; loading?: boolean }) {
    return <TickerChart history={[]} priceData={[]} capData={data} loading={loading} defaultSeries="market_cap" />
}
