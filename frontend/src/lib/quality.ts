import api from "@/lib/api"
import type { Severity } from "@/components/terminal/primitives"

export interface Finding {
    check: string
    severity: Severity
    message: string
    cik: string | null
    ticker: string | null
    details: Record<string, unknown>
}

/** Checks that make a ranking row's % change untrustworthy, with a short badge label. */
export const RANKING_FLAG_CHECKS: Record<string, string> = {
    stats_split_contaminated: "Split",
    stats_window_mismatch: "Window",
    stats_arithmetic: "Math",
    stats_latest_mismatch: "Latest",
}

export interface RankingFlag {
    label: string
    title: string
}

function labelFor(f: Finding): string {
    if (f.check === "stats_split_contaminated") {
        const kind = (f.details?.jump as { kind?: string } | undefined)?.kind
        if (kind === "units_error") return "Units"
        if (kind === "spike") return "Spike"
        return "Split"
    }
    return RANKING_FLAG_CHECKS[f.check] ?? "Check"
}

/** ticker -> flag, from the latest audit. Empty map when no audit has been run. */
export async function fetchRankingFlags(): Promise<Record<string, RankingFlag>> {
    const out: Record<string, RankingFlag> = {}
    await Promise.all(Object.keys(RANKING_FLAG_CHECKS).map(async (check) => {
        try {
            const res = await api.get("/data-quality/findings", { params: { check, limit: 1000 } })
            for (const f of res.data.items as Finding[]) {
                if (f.ticker && !out[f.ticker]) out[f.ticker] = { label: labelFor(f), title: f.message }
            }
        } catch {
            /* audit not run yet: no flags */
        }
    }))
    return out
}

export async function fetchTickerFindings(ticker: string): Promise<Finding[] | null> {
    try {
        const res = await api.get("/data-quality/findings", { params: { ticker, limit: 200 } })
        return res.data.items as Finding[]
    } catch {
        return null
    }
}
