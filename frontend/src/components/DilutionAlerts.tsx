"use client"

import Link from "next/link"
import { Empty, FilingLink, Panel, Tag } from "@/components/terminal/primitives"
import { offeringAmount, parseDate } from "@/lib/format"
import { tickerPrefetch } from "@/lib/demo"

export interface DilutionAlert {
    date: string
    ticker?: string
    category: string
    text: string
    source_filing: string
    source_url?: string
    amount?: number
    amount_text?: string
}

interface DilutionAlertsProps {
    alerts: DilutionAlert[]
    showTicker?: boolean
    title?: string
}

function dilutionClass(category: string): "Active" | "Potential" {
    if (category.includes('ATM') || category.includes('Series') || category.includes('Direct') || category.includes('SBC')) return 'Active'
    return 'Potential'
}

export function DilutionAlerts({ alerts, showTicker = false, title = "Dilution alerts, last 30 days" }: DilutionAlertsProps) {
    const rows = [...(alerts || [])].sort((a, b) => String(b.date).localeCompare(String(a.date)))
    return (
        <Panel title={title} meta={rows.length ? `${rows.length} filings` : undefined}>
            {rows.length === 0 ? (
                <Empty>No new dilution filings for tracked tickers.</Empty>
            ) : (
                <div className="max-h-[420px] overflow-auto">
                    <table className="data-table">
                        <thead>
                            <tr>
                                <th>Date</th>
                                {showTicker && <th>Ticker</th>}
                                <th>Instrument</th>
                                <th>Class</th>
                                <th className="r">Amount</th>
                                <th className="r">Filing</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map((a, i) => {
                                const cls = dilutionClass(a.category)
                                const fresh = Date.now() - parseDate(a.date).getTime() < 7 * 86400_000
                                return (
                                    <tr key={i}>
                                        <td className="num whitespace-nowrap text-muted-foreground">
                                            {a.date}{fresh && <Tag tone="accent" className="ml-2">New</Tag>}
                                        </td>
                                        {showTicker && (
                                            <td>
                                                <Link href={`/dashboard/ticker/${a.ticker}`} prefetch={tickerPrefetch(a.ticker)} className="font-medium hover:text-[var(--dt-accent)]">{a.ticker}</Link>
                                            </td>
                                        )}
                                        <td className="max-w-[260px] truncate" title={a.text}>{a.category}</td>
                                        <td className="text-muted-foreground">{cls}</td>
                                        <td className="r num font-medium">{offeringAmount(a)}</td>
                                        <td className="r"><FilingLink url={a.source_url} label={a.source_filing} /></td>
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
