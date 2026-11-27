'use client'

import Link from "next/link"
import { Empty, Panel, Tag } from "@/components/terminal/primitives"
import { fmtCompact, fmtPct, fmtUsd } from "@/lib/format"
import { tickerPrefetch } from "@/lib/demo"

export interface RankingItem {
    ticker: string
    title: string
    cik: string
    change_pct: number
    start_date: string
    latest_shares: number
    last_updated: string
    market_cap?: number | null
    raw_change_pct?: number | null
    split_adjusted?: boolean
    spac?: boolean
    adjustments?: string[]
    flags?: { kind: string; label: string; date: string; detail: string }[]
}

interface RankingsTableProps {
    title: string
    items: RankingItem[]
    type: 'buyback' | 'dilution'
    loading?: boolean
}

export function RankingsTable({ title, items, type, loading }: RankingsTableProps) {
    const flagged = items.filter((i) => i.flags?.length).length
    return (
        <Panel
            title={title}
            meta={loading ? "Loading…" : `${items.length} companies · split-adjusted${flagged ? ` · ${flagged} flagged` : ""}`}
        >
            {loading ? (
                <div className="p-4 space-y-2">{Array.from({ length: 10 }).map((_, i) => <div key={i} className="skeleton h-6" />)}</div>
            ) : items.length === 0 ? (
                <Empty>No companies match these filters.</Empty>
            ) : (
                <div className="max-h-[70vh] overflow-auto">
                    <table className="data-table">
                        <thead>
                            <tr>
                                <th className="w-10 r">#</th>
                                <th>Company</th>
                                <th className="r">Change</th>
                                <th className="r hidden sm:table-cell">Shares</th>
                                <th className="r hidden md:table-cell">Mkt cap</th>
                                <th className="r hidden 2xl:table-cell">Since</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map((item, index) => {
                                const flag = item.flags?.[0]
                                return (
                                    <tr key={item.ticker} className={flag ? "opacity-70" : undefined}>
                                        <td className="r num text-faint">{index + 1}</td>
                                        <td className="max-w-[240px]">
                                            <div className="flex items-center gap-2">
                                                <Link href={`/dashboard/ticker/${item.ticker}`} prefetch={tickerPrefetch(item.ticker)} className="font-medium hover:text-[var(--dt-accent)]">
                                                    {item.ticker}
                                                </Link>
                                                {item.split_adjusted && (
                                                    <span className="text-[11px] text-faint" title={`Change restated for: ${(item.adjustments ?? []).join("; ")}. Unadjusted: ${fmtPct(item.raw_change_pct, 1)}`}>adj.</span>
                                                )}
                                                {item.spac && (
                                                    <Tag title="SPAC (blank-check company): share changes reflect trust redemptions or the merger, not buybacks or ordinary dilution">SPAC</Tag>
                                                )}
                                                {flag && (
                                                    <Tag tone="accent" title={item.flags!.map((f) => `${f.label} ${f.date}: ${f.detail}`).join("\n")}>{flag.label}?</Tag>
                                                )}
                                            </div>
                                            <div className="truncate text-xs text-faint" title={item.title}>{item.title}</div>
                                        </td>
                                        <td className={`r num font-medium ${type === 'buyback' ? 'text-up' : 'text-down'}`}>
                                            {fmtPct(item.change_pct, 1)}
                                        </td>
                                        <td className="r num hidden sm:table-cell text-muted-foreground">{fmtCompact(item.latest_shares, 1)}</td>
                                        <td className="r num hidden md:table-cell text-muted-foreground">{item.market_cap ? fmtUsd(item.market_cap) : "—"}</td>
                                        <td className="r num hidden 2xl:table-cell text-faint">{item.start_date || "—"}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            )}
        </Panel>
    )
}
