"use client"

import { useEffect, useMemo, useState } from "react"
import Link from "next/link"
import { Button } from "@/components/ui/button"
import { Check, Plus, RefreshCw } from "lucide-react"
import { TickerChart } from "@/components/charts/TickerChart"
import { Empty, FilingLink, Panel, SeverityDot, Stat, Tag } from "@/components/terminal/primitives"
import {
    fmtCompact, fmtDate, fmtPct, fmtPrice, fmtUsd, offeringAmount, parseDate, shareChangeClass,
} from "@/lib/format"
import { fetchTickerFindings, type Finding } from "@/lib/quality"

interface TickerDetailViewProps {
    tickerData: any
    history: any[]
    priceData: any[]
    capData: any[]
    offerings?: any[]
    isAdded: boolean
    onAdd: () => void
    loading?: boolean
    onRefreshOfferings?: () => void
    onSync?: () => Promise<void> | void
}

/**
 * Size of a potential-dilution instrument relative to the company today:
 * dollar instruments vs market cap, share instruments vs shares outstanding.
 */
function dilutionPct(o: any, marketCap?: number, sharesOut?: number): { value: number; basis: string } | null {
    if (o.amount && marketCap && marketCap > 0) return { value: (o.amount / marketCap) * 100, basis: "of market cap" }
    const m = String(o.amount_text || "").match(/([\d,]+(?:\.\d+)?)\s*shares/i)
    if (m && sharesOut && sharesOut > 0) {
        const shares = parseFloat(m[1].replace(/,/g, ""))
        if (shares > 0) return { value: (shares / sharesOut) * 100, basis: "of shares outstanding" }
    }
    return null
}

function isRecent(date: string, days = 31): boolean {
    const d = parseDate(date)
    return !Number.isNaN(d.getTime()) && Date.now() - d.getTime() < days * 86400_000
}

function latestBy<T extends { date: string }>(rows: T[]): T | null {
    let best: T | null = null
    for (const r of rows || []) if (!best || parseDate(r.date) > parseDate(best.date)) best = r
    return best
}

function DilutionTable({ items, showPct, marketCap, sharesOut }: {
    items: any[]; showPct?: boolean; marketCap?: number; sharesOut?: number
}) {
    const rows = [...items].sort((a, b) => String(b.date).localeCompare(String(a.date)))
    return (
        <div className="max-h-[340px] overflow-auto">
            <table className="data-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Instrument</th>
                        <th className="r">Amount</th>
                        {showPct && <th className="r" title="Dollar instruments vs market cap; share instruments vs shares outstanding">Size</th>}
                        <th className="r">Filing</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map((o, i) => {
                        const dp = showPct ? dilutionPct(o, marketCap, sharesOut) : null
                        return (
                            <tr key={`${o.source_filing}-${i}`}>
                                <td className="num whitespace-nowrap text-muted-foreground">
                                    {o.date}
                                    {isRecent(o.date) && <Tag tone="accent" className="ml-2">New</Tag>}
                                </td>
                                <td className="max-w-[260px]">
                                    <div className="truncate" title={o.description || o.text}>{o.category}</div>
                                    {o.description && <div className="truncate text-xs text-faint" title={o.description}>{o.description}</div>}
                                </td>
                                <td className="r num font-medium">{offeringAmount(o)}</td>
                                {showPct && (
                                    <td className="r num text-muted-foreground" title={dp ? `${dp.value.toFixed(2)}% ${dp.basis}` : undefined}>
                                        {dp ? (dp.value < 0.1 ? "<0.1%" : `${dp.value.toFixed(1)}%`) : "—"}
                                    </td>
                                )}
                                <td className="r"><FilingLink url={o.source_url} label={o.source_filing || "SEC"} /></td>
                            </tr>
                        )
                    })}
                </tbody>
            </table>
        </div>
    )
}

export function TickerDetailView({
    tickerData, history, priceData, capData, offerings = [],
    isAdded, onAdd, loading = false, onRefreshOfferings, onSync,
}: TickerDetailViewProps) {
    const ticker: string = tickerData?.ticker ?? ""
    const stats = tickerData?.stats ?? {}
    const [findings, setFindings] = useState<Finding[] | null | undefined>(undefined)

    useEffect(() => {
        if (!ticker) return
        let cancelled = false
        setFindings(undefined)
        fetchTickerFindings(ticker).then((f) => { if (!cancelled) setFindings(f) })
        return () => { cancelled = true }
    }, [ticker])

    const lastPrice = useMemo(() => latestBy(priceData || []), [priceData])
    const lastCap = useMemo(() => latestBy(capData || []), [capData])
    const marketCap: number | undefined = lastCap?.market_cap || undefined
    const sharesOut: number | undefined = stats.latest_shares || lastCap?.shares || undefined

    const active = (offerings || []).filter((o: any) => o.status === "Active")
    const potential = (offerings || []).filter((o: any) => o.status === "Potential")
    const potentialParts = potential.map((o: any) => dilutionPct(o, marketCap, sharesOut)).filter(Boolean) as { value: number }[]
    const potentialTotal = potentialParts.reduce((s, p) => s + p.value, 0)

    const byInstrument = useMemo(() => {
        const m = new Map<string, { category: string; status: Set<string>; count: number; latest: string; dollars: number }>()
        for (const o of offerings || []) {
            const k = o.category || "Other"
            const e = m.get(k) ?? { category: k, status: new Set<string>(), count: 0, latest: "", dollars: 0 }
            e.count += 1
            if (o.status) e.status.add(o.status)
            if (String(o.date) > e.latest) e.latest = String(o.date)
            if (o.amount) e.dollars += o.amount
            m.set(k, e)
        }
        return [...m.values()].sort((a, b) => b.latest.localeCompare(a.latest))
    }, [offerings])

    const historyDesc = useMemo(
        () => [...(history || [])].sort((a, b) => String(b.date).localeCompare(String(a.date))), [history])

    if (!tickerData) return null

    return (
        <div className="space-y-4">
            {/* 1. Header */}
            <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
                <div className="min-w-0">
                    <div className="flex items-baseline gap-3">
                        <h1 className="text-2xl font-semibold tracking-tight">{ticker}</h1>
                        {tickerData.cik && <span className="text-xs text-faint num">CIK {tickerData.cik}</span>}
                    </div>
                    <p className="text-[13px] text-muted-foreground truncate">{tickerData.title}</p>
                </div>
                <div className="flex items-end gap-6">
                    <div className="text-right">
                        <div className="text-2xl font-semibold num">{fmtPrice(lastPrice?.price)}</div>
                        <div className="text-xs text-faint num">{lastPrice ? `Last price · ${fmtDate(lastPrice.date)}` : "No price data"}</div>
                    </div>
                    <div className="flex gap-2">
                        {onSync && (
                            <Button size="sm" variant="outline" onClick={() => onSync()} disabled={loading} className="shadow-none">
                                <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
                            </Button>
                        )}
                        <Button size="sm" variant={isAdded ? "outline" : "default"} onClick={onAdd} disabled={isAdded} className="shadow-none">
                            {isAdded ? <Check /> : <Plus />} {isAdded ? "In portfolio" : "Add to portfolio"}
                        </Button>
                    </div>
                </div>
            </div>

            {/* 2. Key metrics */}
            <div className="panel overflow-hidden grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-px !bg-[var(--dt-border)]">
                <Stat label="Shares out" value={fmtCompact(stats.latest_shares)} sub={stats.last_date ? `as of ${stats.last_date}` : undefined} />
                <Stat label="1Q change" value={fmtPct(stats.change_pct_1q, 2)} valueClass={shareChangeClass(stats.change_pct_1q)} />
                <Stat label="1Y change" value={fmtPct(stats.change_pct_1y, 2)} valueClass={shareChangeClass(stats.change_pct_1y)}
                    sub={stats.date_1y ? `vs ${stats.date_1y}` : undefined} />
                <Stat label="5Y change" value={fmtPct(stats.change_pct_5y, 1)} valueClass={shareChangeClass(stats.change_pct_5y)} />
                <Stat label="Market cap" value={fmtUsd(marketCap, 2)} />
                <Stat label="Potential dilution" value={potentialParts.length ? `~${potentialTotal.toFixed(1)}%` : "—"}
                    sub={potentialParts.length ? `${potentialParts.length} instruments, may overlap` : "none recorded"} />
            </div>

            {Array.isArray(stats.splits) && stats.splits.length > 0 && (
                <p className="text-xs text-faint num -mt-2">
                    Split-adjusted to the latest share basis:{" "}
                    {stats.splits.map((sp: any, i: number) => (
                        <span key={i} title={`observed x${sp.observed_ratio} between filings; source ${sp.source}${sp.exact ? "" : " (approximate: same-quarter issuance)"}`}>
                            {i > 0 && "; "}{sp.label} effective {sp.event_date ?? sp.effective_date} ({sp.source === "yfinance" ? "Yahoo Finance split record" : sp.source === "units" ? "units change in filings" : "detected from filings"})
                        </span>
                    ))}
                    {stats.raw_change_pct_1y != null && Math.abs((stats.raw_change_pct_1y ?? 0) - (stats.change_pct_1y ?? 0)) > 0.05 &&
                        ` · unadjusted 1Y was ${fmtPct(stats.raw_change_pct_1y, 1)}`}
                </p>
            )}

            {/* 3. Share-count history */}
            <Panel title="History">
                <TickerChart history={history} priceData={priceData} capData={capData} loading={loading} />
            </Panel>

            {/* 4. AI summary */}
            <div className="panel border-l-2 border-l-[var(--dt-accent)] px-4 py-3">
                <div className="label">Dilution summary</div>
                {stats.dilution_summary ? (
                    <p className="mt-1.5 text-[13px] leading-6 text-[var(--dt-text)] max-w-4xl">{stats.dilution_summary}</p>
                ) : (
                    <p className="mt-1.5 text-[13px] text-muted-foreground">
                        {loading ? "Loading…" : "No summary yet. Summaries are written by the AI dilution job after it reads this company's filings."}
                    </p>
                )}
            </div>

            {/* 5. Active and potential dilution */}
            <div className="grid gap-4 xl:grid-cols-2">
                <Panel title="Active dilution" meta={`${active.length} records`}
                    actions={onRefreshOfferings && <button onClick={onRefreshOfferings} className="text-xs text-muted-foreground hover:text-foreground">Reload</button>}>
                    {loading && active.length === 0 ? <Empty>Loading…</Empty>
                        : active.length === 0 ? <Empty>No active offerings recorded for {ticker}.</Empty>
                            : <DilutionTable items={active} />}
                </Panel>
                <Panel title="Potential dilution" meta={`${potential.length} records`}
                    actions={potentialParts.length > 0 && <span className="text-xs text-muted-foreground num" title="Sum of each instrument's size; instruments can overlap (e.g. warrants under a shelf), so treat as an upper bound.">Total ~{potentialTotal.toFixed(1)}%</span>}>
                    {loading && potential.length === 0 ? <Empty>Loading…</Empty>
                        : potential.length === 0 ? <Empty>No shelf, warrant, convertible or rights capacity recorded.</Empty>
                            : <DilutionTable items={potential} showPct marketCap={marketCap} sharesOut={sharesOut} />}
                </Panel>
            </div>

            {byInstrument.length > 0 && (
                <Panel title="By instrument">
                    <table className="data-table">
                        <thead>
                            <tr><th>Instrument</th><th>Status</th><th className="r">Records</th><th className="r">Dollar amount</th><th className="r">Latest</th></tr>
                        </thead>
                        <tbody>
                            {byInstrument.map((r) => (
                                <tr key={r.category}>
                                    <td>{r.category}</td>
                                    <td className="text-muted-foreground">{[...r.status].join(", ") || "—"}</td>
                                    <td className="r num">{r.count}</td>
                                    <td className="r num">{r.dollars ? fmtUsd(r.dollars) : "—"}</td>
                                    <td className="r num text-muted-foreground">{r.latest}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </Panel>
            )}

            {/* 6. Data-quality flags for this ticker */}
            <Panel title="Data quality" meta={findings ? `${findings.length} findings` : undefined}
                actions={<Link href="/dashboard/data-quality" className="text-xs text-muted-foreground hover:text-foreground">Full audit</Link>}>
                {findings === undefined ? <Empty>Loading…</Empty>
                    : findings === null ? <Empty>No audit report available. Run <code className="text-xs">python -m app.data_quality audit</code>.</Empty>
                        : findings.length === 0 ? <Empty>The latest audit found nothing for {ticker}.</Empty>
                            : (
                                <div className="max-h-[260px] overflow-auto">
                                    <table className="data-table">
                                        <thead><tr><th className="w-24">Severity</th><th className="w-56">Check</th><th>Finding</th></tr></thead>
                                        <tbody>
                                            {findings.map((f, i) => (
                                                <tr key={i}>
                                                    <td><SeverityDot severity={f.severity} /></td>
                                                    <td className="text-xs text-muted-foreground">{f.check}</td>
                                                    <td className="num">{f.message}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )}
            </Panel>

            {/* Filing-level share counts: as filed vs split-adjusted */}
            <Panel title="Share count by filing" meta={`${historyDesc.length} filings · adjusted to latest share basis`}>
                {historyDesc.length === 0 ? <Empty>{loading ? "Loading…" : "No share-count history."}</Empty> : (
                    <div className="max-h-[320px] overflow-auto">
                        <table className="data-table">
                            <thead>
                                <tr><th>Period end</th><th>Form</th><th className="r">As filed</th><th className="r">Adjusted</th><th className="r">Change</th><th>Note</th></tr>
                            </thead>
                            <tbody>
                                {historyDesc.map((row: any, i: number) => {
                                    const excluded = row.status === "excluded"
                                    // change vs the previous (older) point that is actually used
                                    const prev = historyDesc.slice(i + 1).find((r: any) => r.status !== "excluded")
                                    const chg = !excluded && prev?.shares ? (row.shares / prev.shares - 1) * 100 : null
                                    const factor = Number(row.factor ?? 1)
                                    const note = excluded ? `Not used: ${row.note ?? "bad data point"}`
                                        : row.status === "repaired" ? `Units corrected: ${row.note ?? ""}`
                                            : factor !== 1 ? `Split-adjusted ×${Number(factor.toPrecision(6))}` : ""
                                    return (
                                        <tr key={`${row.date}-${i}`} className={excluded ? "text-faint" : undefined}>
                                            <td className="num">{row.date}</td>
                                            <td>
                                                {row.source_url
                                                    ? <a href={row.source_url} target="_blank" rel="noopener noreferrer" className="text-[var(--dt-accent)] hover:underline">{row.source}</a>
                                                    : row.source}
                                            </td>
                                            <td className="r num text-muted-foreground">{(row.raw_shares ?? row.shares)?.toLocaleString("en-US") ?? "—"}</td>
                                            <td className={`r num ${excluded ? "line-through" : ""}`}>{row.shares?.toLocaleString("en-US") ?? "—"}</td>
                                            <td className={`r num ${shareChangeClass(chg)}`}>{chg === null ? "—" : fmtPct(chg, 2)}</td>
                                            <td className="text-xs text-faint max-w-[280px] truncate" title={note}>{note}</td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </Panel>
        </div>
    )
}
