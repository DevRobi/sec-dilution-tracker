"use client"

import Link from "next/link"
import { usePathname, useRouter } from "next/navigation"
import { TickerAutocomplete } from "@/components/TickerAutocomplete"
import { cn } from "@/lib/utils"
import { IS_DEMO, demoNotice } from "@/lib/demo"

const NAV = [
    { href: "/dashboard", label: "Portfolio", match: (p: string) => p === "/dashboard" },
    { href: "/dashboard/rankings", label: "Rankings", match: (p: string) => p.startsWith("/dashboard/rankings") },
    { href: "/dashboard/search", label: "Search", match: (p: string) => p.startsWith("/dashboard/search") || p.startsWith("/dashboard/ticker") },
    { href: "/dashboard/data-quality", label: "Data quality", match: (p: string) => p.startsWith("/dashboard/data-quality") },
]

export function Wordmark() {
    return (
        <Link href="/dashboard" className="flex items-center gap-2 shrink-0">
            <span className="h-4 w-4 rounded-[3px] border border-[var(--dt-accent)] grid place-items-center">
                <span className="h-1.5 w-1.5 bg-[var(--dt-accent)]" />
            </span>
            <span className="text-[14px] font-semibold tracking-tight">Dilution Terminal</span>
        </Link>
    )
}

export default function TerminalLayout({ children }: { children: React.ReactNode }) {
    const pathname = usePathname()
    const router = useRouter()

    const handleLogout = () => {
        if (IS_DEMO) {
            demoNotice("You are browsing as a read-only demo user; accounts are off in this snapshot.")
            return
        }
        localStorage.removeItem("token")
        router.push("/")
    }

    return (
        <div className="min-h-screen bg-background">
            <nav className="sticky top-0 z-50 border-b border-border bg-[var(--dt-bg)]">
                <div className="mx-auto max-w-[1400px] h-12 px-4 md:px-6 flex items-center gap-6">
                    <Wordmark />
                    <div className="flex items-center gap-1 overflow-x-auto">
                        {NAV.map((n) => {
                            const active = n.match(pathname)
                            return (
                                <Link
                                    key={n.href}
                                    href={n.href}
                                    className={cn(
                                        "relative h-12 px-3 inline-flex items-center text-[13px] whitespace-nowrap transition-colors",
                                        active ? "text-foreground" : "text-muted-foreground hover:text-foreground"
                                    )}
                                >
                                    {n.label}
                                    {active && <span className="absolute inset-x-3 bottom-0 h-px bg-[var(--dt-accent)]" />}
                                </Link>
                            )
                        })}
                    </div>
                    <div className="ml-auto flex items-center gap-3">
                        <div className="hidden md:block w-64">
                            <TickerAutocomplete
                                compact
                                placeholder="Go to ticker"
                                onSelect={(t) => router.push(`/dashboard/ticker/${t}`)}
                            />
                        </div>
                        <button onClick={handleLogout} className="text-[13px] text-muted-foreground hover:text-foreground">
                            {IS_DEMO ? "Demo user" : "Sign out"}
                        </button>
                    </div>
                </div>
            </nav>
            <main className="mx-auto max-w-[1400px] px-4 md:px-6 py-6">{children}</main>
        </div>
    )
}
