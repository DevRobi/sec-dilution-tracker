"use client"

import { TickerChart } from "@/components/charts/TickerChart"

/** Kept for compatibility; the ticker page uses TickerChart directly. */
export function ShareHistoryChart({ data, loading }: { data: any[]; loading?: boolean }) {
    return <TickerChart history={data} priceData={[]} capData={[]} loading={loading} defaultSeries="shares" />
}
