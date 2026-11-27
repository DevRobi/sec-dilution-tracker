'use client'

import { useEffect, useState } from 'react'
import api from '@/lib/api'
import { RankingsTable, type RankingItem } from '@/components/RankingsTable'
import { Empty, PageHeader, Panel, Segmented } from '@/components/terminal/primitives'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { IS_DEMO, SNAPSHOT_MISS_TEXT, isSnapshotMiss } from '@/lib/demo'

// Market-cap thresholds. "any" means no bound.
const CAP_OPTIONS: { value: string; label: string }[] = [
    { value: "any", label: "Any" },
    { value: "50000000", label: "$50M" },
    { value: "300000000", label: "$300M" },
    { value: "1000000000", label: "$1B" },
    { value: "2000000000", label: "$2B" },
    { value: "10000000000", label: "$10B" },
    { value: "50000000000", label: "$50B" },
    { value: "200000000000", label: "$200B" },
]

function ToolbarField({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div className="flex items-center gap-2">
            <span className="label">{label}</span>
            {children}
        </div>
    )
}

const triggerCls = "h-7 w-auto min-w-[96px] gap-2 px-2 text-[13px] bg-[var(--dt-bg)] shadow-none border-border"

export default function RankingsPage() {
    const [years, setYears] = useState(1)
    const [limit, setLimit] = useState(50)
    const [minCap, setMinCap] = useState<string>("any")
    const [maxCap, setMaxCap] = useState<string>("any")
    const [view, setView] = useState<"both" | "buyback" | "dilution">("both")
    const [hideFlagged, setHideFlagged] = useState(false)
    const [includeSpacs, setIncludeSpacs] = useState(false)

    const [buybacks, setBuybacks] = useState<RankingItem[]>([])
    const [dilution, setDilution] = useState<RankingItem[]>([])
    const [loading, setLoading] = useState(true)
    const [missing, setMissing] = useState(false)


    useEffect(() => {
        let cancelled = false
        const run = async () => {
            setLoading(true)
            try {
                let q = `years=${years}&limit=${limit}`
                if (minCap !== "any") q += `&min_cap=${minCap}`
                if (maxCap !== "any") q += `&max_cap=${maxCap}`
                if (includeSpacs) q += `&include_spacs=true`
                const [b, d] = await Promise.all([
                    api.get(`/rankings?type=buyback&${q}`),
                    api.get(`/rankings?type=dilution&${q}`),
                ])
                if (!cancelled) { setBuybacks(b.data); setDilution(d.data); setMissing(false) }
            } catch (e) {
                if (IS_DEMO && isSnapshotMiss(e)) {
                    if (!cancelled) { setBuybacks([]); setDilution([]); setMissing(true) }
                } else {
                    console.error("Failed to fetch rankings", e)
                }
            } finally {
                if (!cancelled) setLoading(false)
            }
        }
        run()
        return () => { cancelled = true }
    }, [years, limit, minCap, maxCap, includeSpacs])

    const filter = (rows: RankingItem[]) => hideFlagged ? rows.filter((r) => !r.flags?.length) : rows
    const flagCount = [...buybacks, ...dilution].filter((r) => r.flags?.length).length

    return (
        <div className="space-y-4">
            <PageHeader
                title="Rankings"
                description="Largest changes in shares outstanding from SEC filings, restated for stock splits so a reverse split is not counted as a buyback. SPACs are excluded by default. A tag marks rows whose change is still uncertain after adjustment."
            />

            <div className="panel flex flex-wrap items-center gap-x-6 gap-y-3 px-3 py-2">
                <ToolbarField label="Period">
                    <Segmented<number> value={years} onChange={setYears}
                        options={[1, 2, 3, 4, 5].map((y) => ({ value: y, label: `${y}Y` }))} />
                </ToolbarField>
                <ToolbarField label="Market cap">
                    <Select value={minCap} onValueChange={setMinCap}>
                        <SelectTrigger className={triggerCls}><SelectValue placeholder="Min" /></SelectTrigger>
                        <SelectContent>
                            {CAP_OPTIONS.map((o) => <SelectItem key={`min-${o.value}`} value={o.value}>{o.value === "any" ? "Any min" : o.label}</SelectItem>)}
                        </SelectContent>
                    </Select>
                    <span className="text-faint">to</span>
                    <Select value={maxCap} onValueChange={setMaxCap}>
                        <SelectTrigger className={triggerCls}><SelectValue placeholder="Max" /></SelectTrigger>
                        <SelectContent>
                            {CAP_OPTIONS.map((o) => <SelectItem key={`max-${o.value}`} value={o.value}>{o.value === "any" ? "Any max" : o.label}</SelectItem>)}
                        </SelectContent>
                    </Select>
                </ToolbarField>
                <ToolbarField label="Rows">
                    <Select value={String(limit)} onValueChange={(v) => setLimit(Number(v))}>
                        <SelectTrigger className={triggerCls}><SelectValue /></SelectTrigger>
                        <SelectContent>
                            {[10, 20, 50, 100, 200].map((n) => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}
                        </SelectContent>
                    </Select>
                </ToolbarField>
                <div className="flex items-center gap-4 lg:ml-auto">
                    <label className="flex items-center gap-2 text-[13px] text-muted-foreground cursor-pointer select-none"
                        title={flagCount ? `${flagCount} rows still unreliable after split adjustment` : "No flagged rows"}>
                        <input type="checkbox" className="accent-[var(--dt-accent)]" checked={hideFlagged}
                            onChange={(e) => setHideFlagged(e.target.checked)} disabled={!flagCount} />
                        Hide flagged
                    </label>
                    <label className="flex items-center gap-2 text-[13px] text-muted-foreground cursor-pointer select-none"
                        title="SPACs (blank-check companies, SEC industry code 6770) are excluded by default: trust redemptions shrink their share count without being buybacks.">
                        <input type="checkbox" className="accent-[var(--dt-accent)]" checked={includeSpacs}
                            onChange={(e) => setIncludeSpacs(e.target.checked)} />
                        Include SPACs
                    </label>
                    <Segmented<"both" | "buyback" | "dilution"> value={view} onChange={setView}
                        options={[{ value: "both", label: "Both" }, { value: "buyback", label: "Buybacks" }, { value: "dilution", label: "Dilution" }]} />
                </div>
            </div>

            {missing && (
                <Panel>
                    <Empty>
                        This filter combination is {SNAPSHOT_MISS_TEXT.charAt(0).toLowerCase() + SNAPSHOT_MISS_TEXT.slice(1)}
                        <span className="block mt-1 text-xs text-faint">Every period, market-cap range and SPAC setting is included at 10, 20 or 50 rows; 100 and 200 rows only with no market-cap filter.</span>
                    </Empty>
                </Panel>
            )}

            {!missing && <div className={`grid gap-4 ${view === "both" ? "lg:grid-cols-2" : ""}`}>
                {view !== "dilution" && (
                    <RankingsTable title="Largest buybacks" items={filter(buybacks)} type="buyback" loading={loading} />
                )}
                {view !== "buyback" && (
                    <RankingsTable title="Largest dilution" items={filter(dilution)} type="dilution" loading={loading} />
                )}
            </div>}
            <p className="text-xs text-faint">
                Changes use split-adjusted share counts (splits detected from the filing series, or Yahoo Finance split records where cached).
                &quot;adj.&quot; marks a row restated for a split; hover it for the unadjusted figure. Sanity caps of −50% / +500% still apply.
            </p>
        </div>
    )
}
