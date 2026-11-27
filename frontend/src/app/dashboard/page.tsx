"use client"

import { useState } from "react"
import Link from "next/link"
import api from "@/lib/api"
import { Button } from "@/components/ui/button"
import { RefreshCw, X } from "lucide-react"
import { TickerAutocomplete } from "@/components/TickerAutocomplete"
import { DilutionAlerts } from "@/components/DilutionAlerts"
import { Empty, PageHeader, Panel } from "@/components/terminal/primitives"
import { usePortfolio } from "@/contexts/PortfolioContext"
import { fmtCompact, fmtPct, shareChangeClass } from "@/lib/format"
import { IS_DEMO, demoNotice } from "@/lib/demo"

const DEMO_EDIT_NOTICE = "Portfolio edits are disabled in this read-only snapshot."

export default function DashboardPage() {
    const { items, alerts, loading, refreshPortfolio, addItemOptimistic, removeItemOptimistic } = usePortfolio()
    const [status, setStatus] = useState("")

    const handleDelete = async (ticker: string) => {
        if (IS_DEMO) return demoNotice(DEMO_EDIT_NOTICE)
        if (!confirm(`Remove ${ticker} from your portfolio?`)) return
        const removed = items.find(i => i.ticker === ticker)
        removeItemOptimistic(ticker)
        try {
            await api.delete(`/portfolio/${ticker}`)
            // Forced revalidation so alerts for the removed ticker disappear too.
            refreshPortfolio(true)
        } catch (e) {
            console.error(e)
            if (removed) addItemOptimistic(removed)
        }
    }

    const handleAddTicker = async (ticker: string) => {
        if (!ticker) return
        if (IS_DEMO) return demoNotice(DEMO_EDIT_NOTICE)
        setStatus(`Adding ${ticker}…`)
        try {
            addItemOptimistic({
                cik: "",
                ticker,
                title: "Loading…",
                stats: { latest_shares: 0, change_pct_1q: 0, change_pct_1y: 0, change_pct_5y: 0, dilution_summary: undefined },
            })
            await api.post(`/portfolio/${ticker}`)
            refreshPortfolio(true)
            setStatus("")
        } catch (e: any) {
            console.error("Failed to add ticker:", e)
            removeItemOptimistic(ticker)
            setStatus(e.response?.status === 404 ? "Ticker is not in the tracked universe yet." : "Could not add ticker.")
            setTimeout(() => setStatus(""), 3000)
        }
    }

    return (
        <div className="space-y-4">
            <PageHeader
                title="Portfolio"
                description={IS_DEMO
                    ? "Demo user's portfolio: the companies included in this snapshot. A rising share count (red) is dilution; a falling one (green) is a buyback."
                    : "Tracked companies and their share-count trend. A rising share count (red) is dilution; a falling one (green) is a buyback."}
                actions={
                    <>
                        {status && <span className="text-xs text-muted-foreground">{status}</span>}
                        <div className="w-56">
                            <TickerAutocomplete compact placeholder="Add ticker" onSelect={handleAddTicker} />
                        </div>
                        <Button size="sm" variant="outline" className="h-8 shadow-none" onClick={() => IS_DEMO ? demoNotice("Refresh is disabled: this is a frozen snapshot.") : refreshPortfolio(true)}>
                            <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
                        </Button>
                    </>
                }
            />

            <Panel title="Holdings" meta={items.length ? `${items.length} tickers` : undefined}>
                {loading && items.length === 0 ? (
                    <div className="p-4 space-y-2">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="skeleton h-6" />)}</div>
                ) : items.length === 0 ? (
                    <Empty>
                        No tickers yet. Add one above, or browse <Link href="/dashboard/rankings" className="text-[var(--dt-accent)] hover:underline">rankings</Link>.
                        <span className="block mt-1 text-xs text-faint">Portfolio needs a signed-in account.</span>
                    </Empty>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="data-table">
                            <thead>
                                <tr>
                                    <th>Ticker</th>
                                    <th>Company</th>
                                    <th className="r">Shares out</th>
                                    <th className="r">1Q</th>
                                    <th className="r">1Y</th>
                                    <th className="r">5Y</th>
                                    <th>Summary</th>
                                    <th className="w-10" />
                                </tr>
                            </thead>
                            <tbody>
                                {items.map((item) => (
                                    <tr key={item.ticker}>
                                        <td>
                                            <Link href={`/dashboard/ticker/${item.ticker}`} className="font-medium hover:text-[var(--dt-accent)]">{item.ticker}</Link>
                                        </td>
                                        <td className="max-w-[260px] truncate text-muted-foreground">{item.title}</td>
                                        <td className="r num">{item.stats?.latest_shares ? fmtCompact(item.stats.latest_shares) : "—"}</td>
                                        <td className={`r num ${shareChangeClass(item.stats?.change_pct_1q)}`}>{fmtPct(item.stats?.change_pct_1q, 2)}</td>
                                        <td className={`r num ${shareChangeClass(item.stats?.change_pct_1y)}`}>{fmtPct(item.stats?.change_pct_1y, 2)}</td>
                                        <td className={`r num ${shareChangeClass(item.stats?.change_pct_5y)}`}>{fmtPct(item.stats?.change_pct_5y, 1)}</td>
                                        <td className="max-w-[320px] truncate text-xs text-muted-foreground" title={item.stats?.dilution_summary}>
                                            {item.stats?.dilution_summary || <span className="text-faint">—</span>}
                                        </td>
                                        <td className="r">
                                            <button onClick={() => handleDelete(item.ticker)} aria-label={`Remove ${item.ticker}`}
                                                className="text-faint hover:text-foreground p-1">
                                                <X className="w-3.5 h-3.5" />
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </Panel>

            <DilutionAlerts alerts={alerts} showTicker title="Dilution alerts, last 30 days" />
        </div>
    )
}
