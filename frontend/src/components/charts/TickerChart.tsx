"use client"

import { useMemo, useState } from "react"
import {
    Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts"
import { Segmented } from "@/components/terminal/primitives"
import { fmtCompact, fmtInt, fmtPrice, fmtUsd, parseDate } from "@/lib/format"

export type Series = "shares" | "price" | "market_cap"
type Period = "1y" | "2y" | "5y" | "10y" | "all"

const PERIODS: { value: Period; label: string }[] = [
    { value: "1y", label: "1Y" }, { value: "2y", label: "2Y" }, { value: "5y", label: "5Y" },
    { value: "10y", label: "10Y" }, { value: "all", label: "All" },
]
const YEARS: Record<Period, number | null> = { "1y": 1, "2y": 2, "5y": 5, "10y": 10, all: null }

interface Point { t: number; v: number; source?: string; raw?: number; factor?: number; repaired?: boolean }

const AXIS = { stroke: "var(--dt-border-strong)", tick: { fill: "var(--dt-faint)", fontSize: 11 }, tickLine: false }

function toPoints(rows: any[], series: Series): Point[] {
    const key = series === "shares" ? "shares" : series === "price" ? "price" : "market_cap"
    const pts: Point[] = []
    for (const r of rows || []) {
        // Bad data points (one-off spikes) are flagged by the API and not plotted.
        if (series === "shares" && r?.status === "excluded") continue
        const v = Number(r?.[key])
        if (!r?.date || !Number.isFinite(v) || v <= 0) continue
        const t = parseDate(String(r.date)).getTime()
        if (Number.isFinite(t)) pts.push({
            t, v, source: r.source,
            raw: r.raw_shares ?? undefined, factor: r.factor ?? undefined, repaired: r.status === "repaired",
        })
    }
    pts.sort((a, b) => a.t - b.t)
    if (series === "shares") return pts
    // Price / market cap arrive as hourly bars: keep the last bar of each day.
    const byDay = new Map<string, Point>()
    for (const p of pts) byDay.set(new Date(p.t).toISOString().slice(0, 10), p)
    return [...byDay.values()]
}

function fmtValue(series: Series, v: number): string {
    if (series === "shares") return fmtInt(v)
    if (series === "price") return fmtPrice(v)
    return fmtUsd(v, 2)
}

export function TickerChart({
    history, priceData, capData, loading, defaultSeries = "shares",
}: {
    history: any[]
    priceData: any[]
    capData: any[]
    loading?: boolean
    defaultSeries?: Series
}) {
    const [series, setSeries] = useState<Series>(defaultSeries)
    const [period, setPeriod] = useState<Period>("5y")

    const all = useMemo(() => toPoints(series === "shares" ? history : series === "price" ? priceData : capData, series),
        [series, history, priceData, capData])

    const data = useMemo(() => {
        const yrs = YEARS[period]
        if (!yrs || all.length === 0) return all
        const end = all[all.length - 1].t
        const cutoff = end - yrs * 365.25 * 86400_000
        return all.filter((p) => p.t >= cutoff)
    }, [all, period])

    const spanYears = data.length > 1 ? (data[data.length - 1].t - data[0].t) / (365.25 * 86400_000) : 0
    // Ticks on calendar boundaries: years for long spans, half-years / quarters for short ones.
    const ticks = useMemo(() => {
        if (data.length < 2) return undefined
        const start = new Date(data[0].t), end = data[data.length - 1].t
        const step = spanYears > 12 ? 24 : spanYears > 3 ? 12 : spanYears > 1 ? 6 : 3
        const out: number[] = []
        let y = start.getUTCFullYear(), m = Math.ceil(start.getUTCMonth() / step) * step
        for (let guard = 0; guard < 200; guard++) {
            if (m >= 12) { y += Math.floor(m / 12); m %= 12 }
            const t = Date.UTC(y, m, 1)
            if (t > end) break
            if (t >= data[0].t && (step < 24 || y % 2 === 0)) out.push(t)
            m += step
        }
        return out
    }, [data, spanYears])
    const tickFmt = (t: number) => new Date(t).toLocaleDateString("en-US",
        spanYears > 3 ? { year: "numeric", timeZone: "UTC" } : { month: "short", year: "2-digit", timeZone: "UTC" })

    return (
        <div>
            <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3">
                <Segmented<Series>
                    value={series}
                    onChange={setSeries}
                    options={[
                        { value: "shares", label: "Shares outstanding" },
                        { value: "price", label: "Price" },
                        { value: "market_cap", label: "Market cap" },
                    ]}
                />
                <Segmented<Period> value={period} onChange={setPeriod} options={PERIODS} />
            </div>
            <div className="h-[320px] px-2 pb-2 pt-3">
                {loading && data.length === 0 ? (
                    <div className="skeleton h-full w-full" />
                ) : data.length === 0 ? (
                    <div className="h-full grid place-items-center text-[13px] text-muted-foreground">No data for this series.</div>
                ) : (
                    <ResponsiveContainer width="100%" height="100%">
                        {series === "shares" ? (
                        <BarChart data={data.map((p) => ({ ...p, label: tickFmt(p.t) }))} margin={{ top: 8, right: 16, bottom: 0, left: 8 }} barCategoryGap="12%">
                            <CartesianGrid stroke="var(--dt-border)" vertical={false} />
                            <XAxis dataKey="label" interval="preserveStartEnd" minTickGap={24} {...AXIS} />
                            <YAxis
                                orientation="right" width={64} domain={[0, "auto"]}
                                tickFormatter={(v: number) => fmtCompact(v, 1)}
                                axisLine={false} {...AXIS}
                            />
                            <Tooltip
                                cursor={{ fill: "var(--dt-hover)" }}
                                isAnimationActive={false}
                                content={({ active, payload }) => {
                                    if (!active || !payload?.length) return null
                                    const p = payload[0].payload as Point
                                    return (
                                        <div className="panel px-3 py-2 text-xs">
                                            <div className="text-faint num">{new Date(p.t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "2-digit", timeZone: "UTC" })}</div>
                                            <div className="mt-0.5 text-[13px] font-medium num">{fmtValue(series, p.v)}</div>
                                            {p.source && <div className="text-faint">{p.source}</div>}
                                            {series === "shares" && p.factor !== undefined && p.factor !== 1 && (
                                                <div className="text-faint num">Split-adjusted ×{Number(p.factor.toPrecision(6))} · as filed {fmtInt(p.raw)}</div>
                                            )}
                                            {series === "shares" && p.repaired && <div className="text-faint">Units corrected (filed {fmtInt(p.raw)})</div>}
                                        </div>
                                    )
                                }}
                            />
                            <Bar dataKey="v" fill="var(--dt-accent)" radius={[3, 3, 0, 0]} maxBarSize={36} isAnimationActive={false} />
                        </BarChart>
                        ) : (
                        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                            <CartesianGrid stroke="var(--dt-border)" vertical={false} />
                            <XAxis
                                dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
                                ticks={ticks} tickFormatter={tickFmt} minTickGap={24} {...AXIS}
                            />
                            <YAxis
                                orientation="right" width={64} domain={["auto", "auto"]}
                                tickFormatter={(v: number) => series === "price" ? `$${v.toFixed(v < 10 ? 2 : 0)}` : `$${fmtCompact(v, 1)}`}
                                axisLine={false} {...AXIS}
                            />
                            <Tooltip
                                cursor={{ stroke: "var(--dt-border-strong)" }}
                                isAnimationActive={false}
                                content={({ active, payload }) => {
                                    if (!active || !payload?.length) return null
                                    const p = payload[0].payload as Point
                                    return (
                                        <div className="panel px-3 py-2 text-xs">
                                            <div className="text-faint num">{new Date(p.t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "2-digit", timeZone: "UTC" })}</div>
                                            <div className="mt-0.5 text-[13px] font-medium num">{fmtValue(series, p.v)}</div>
                                            {p.source && <div className="text-faint">{p.source}</div>}
                                        </div>
                                    )
                                }}
                            />
                            <Line
                                type="linear"
                                dataKey="v"
                                stroke="var(--dt-accent)"
                                strokeWidth={1.5}
                                dot={false}
                                activeDot={{ r: 3, strokeWidth: 0, fill: "var(--dt-text)" }}
                                isAnimationActive={false}
                            />
                        </LineChart>
                        )}
                    </ResponsiveContainer>
                )}
            </div>
        </div>
    )
}
