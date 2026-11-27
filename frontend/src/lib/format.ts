// Number / date formatting shared by every page. Render the output inside a
// `.num` element so digits align in columns.

export function fmtCompact(n: number | null | undefined, digits = 2): string {
    if (n === null || n === undefined || Number.isNaN(n)) return "—"
    const abs = Math.abs(n)
    if (abs >= 1e12) return `${(n / 1e12).toFixed(digits)}T`
    if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`
    if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`
    if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`
    return n.toLocaleString("en-US")
}

export function fmtUsd(n: number | null | undefined, digits = 1): string {
    if (n === null || n === undefined || Number.isNaN(n)) return "—"
    if (Math.abs(n) < 1e3) return `$${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}`
    return `$${fmtCompact(n, digits)}`
}

export function fmtPrice(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return "—"
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function fmtPct(n: number | null | undefined, digits = 1, signed = true): string {
    if (n === null || n === undefined || Number.isNaN(n)) return "—"
    const s = Math.abs(n) >= 1000 ? n.toLocaleString("en-US", { maximumFractionDigits: 0 }) : n.toFixed(digits)
    return `${signed && n > 0 ? "+" : ""}${s}%`
}

export function fmtInt(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return "—"
    return Math.round(n).toLocaleString("en-US")
}

/** Share-count change: an increase is dilution (red), a decrease is buyback (green). */
export function shareChangeClass(n: number | null | undefined): string {
    if (n === null || n === undefined || n === 0 || Number.isNaN(n)) return "text-muted-foreground"
    return n > 0 ? "text-down" : "text-up"
}

/** Parse "2026-05-29T23:00:00" / "2026-05-29 23:00:00" / "2026-05-29". */
export function parseDate(s: string): Date {
    return new Date(s.includes("T") || s.length <= 10 ? s : s.replace(" ", "T"))
}

export function fmtDate(s: string | null | undefined): string {
    if (!s) return "—"
    const d = parseDate(s)
    if (Number.isNaN(d.getTime())) return s
    return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "2-digit", timeZone: "UTC" })
}

export function offeringAmount(o: { amount?: number | null; amount_text?: string | null }): string {
    if (o.amount) return fmtUsd(o.amount)
    return o.amount_text || "—"
}
