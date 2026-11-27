// Demo build only (NEXT_DEMO=1). next.config.ts adds "demo.tsx" to
// pageExtensions in demo mode, so this file is the segment layout there and is
// ignored by the normal build. `output: 'export'` needs every [symbol] known at
// build time: one static page per ticker in the recorded snapshot.

export const dynamicParams = false

export function generateStaticParams(): { symbol: string }[] {
    let tickers: { ticker: string }[] = []
    try {
        tickers = JSON.parse(process.env.DEMO_TICKERS || "[]")
    } catch {
        tickers = []
    }
    // Next needs at least one param for an exported dynamic route.
    if (tickers.length === 0) tickers = [{ ticker: "SIDU" }]
    return tickers.map((t) => ({ symbol: t.ticker }))
}

export default function TickerSegmentLayout({ children }: { children: React.ReactNode }) {
    return children
}
