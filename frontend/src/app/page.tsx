"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { TickerAutocomplete } from "@/components/TickerAutocomplete"
import { Wordmark } from "@/components/TerminalLayout"
import { IS_DEMO } from "@/lib/demo"

const SECTIONS = [
    {
        title: "Share-count history",
        body: "Shares outstanding for every 10-K, 10-Q and 20-F, read from SEC XBRL facts, with 1Q to 10Y changes and the filing behind each point.",
    },
    {
        title: "Offerings and overhang",
        body: "ATM programs, shelf registrations, direct offerings, warrants and convertibles extracted from 424B, S-3 and 8-K filings, each linked to its source.",
    },
    {
        title: "Audited, not just scraped",
        body: "A data-quality audit flags late feeds, duplicates, unadjusted splits and numbers that don't reconcile, and the rankings show those flags.",
    },
]

export default function LandingPage() {
    const router = useRouter()
    return (
        <div className="min-h-screen bg-background flex flex-col">
            <header className="border-b border-border">
                <div className="mx-auto max-w-[1100px] h-12 px-4 md:px-6 flex items-center justify-between">
                    <Wordmark />
                    <nav className="flex items-center gap-5 text-[13px]">
                        <Link href="/dashboard/rankings" className="text-muted-foreground hover:text-foreground">Rankings</Link>
                        <Link href="/dashboard/data-quality" className="text-muted-foreground hover:text-foreground">Data quality</Link>
                        {IS_DEMO
                            ? <Link href="/dashboard" className="text-foreground hover:text-[var(--dt-accent)]">Demo portfolio</Link>
                            : <Link href="/auth" className="text-foreground hover:text-[var(--dt-accent)]">Sign in</Link>}
                    </nav>
                </div>
            </header>

            <main className="flex-1">
                <section className="mx-auto max-w-[1100px] px-4 md:px-6 pt-20 pb-16">
                    <p className="label">SEC filings · U.S. equities</p>
                    <h1 className="mt-3 text-[28px] leading-tight font-semibold max-w-2xl">
                        Who is buying back stock, who is issuing it, and how much more could come.
                    </h1>
                    <p className="mt-4 text-[15px] text-muted-foreground max-w-2xl leading-7">
                        Dilution Terminal tracks shares outstanding and offering activity for U.S. listed companies,
                        built directly from EDGAR filings.
                    </p>
                    <div className="mt-8 max-w-md">
                        <TickerAutocomplete
                            placeholder="Look up a ticker, e.g. AAPL"
                            onSelect={(t) => router.push(`/dashboard/ticker/${t}`)}
                        />
                    </div>
                    <div className="mt-4 flex gap-5 text-[13px]">
                        <Link href="/dashboard/rankings" className="text-[var(--dt-accent)] hover:underline">View buyback and dilution rankings</Link>
                        <Link href="/dashboard/ticker/SIDU" className="text-muted-foreground hover:text-foreground">Example: SIDU</Link>
                    </div>
                </section>

                <section className="border-t border-border">
                    <div className="mx-auto max-w-[1100px] px-4 md:px-6 grid md:grid-cols-3">
                        {SECTIONS.map((s, i) => (
                            <div key={s.title} className={`py-8 md:px-6 ${i > 0 ? "border-t md:border-t-0 md:border-l border-border" : "md:pl-0"}`}>
                                <h2 className="text-[14px] font-semibold">{s.title}</h2>
                                <p className="mt-2 text-[13px] leading-6 text-muted-foreground">{s.body}</p>
                            </div>
                        ))}
                    </div>
                </section>
            </main>

            <footer className="border-t border-border">
                <div className="mx-auto max-w-[1100px] px-4 md:px-6 h-12 flex items-center justify-between text-xs text-faint">
                    <span>Data: SEC EDGAR (XBRL and filing text). Not investment advice.</span>
                    <span>Dilution Terminal</span>
                </div>
            </footer>
        </div>
    )
}
