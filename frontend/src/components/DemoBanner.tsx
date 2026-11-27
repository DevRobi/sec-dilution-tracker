"use client"

import { useEffect, useState } from "react"
import {
    DEMO_AS_OF, DEMO_BASE_PATH, DEMO_NOTICE_EVENT, DEMO_REPO_URL, DEMO_TICKERS, IS_DEMO,
    demoNotice, isSnapshotTicker,
} from "@/lib/demo"

const TICKER_PATH = new RegExp(`^${DEMO_BASE_PATH.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/dashboard/ticker/([^/]+)/?$`)

/**
 * Demo build only: slim "live snapshot" banner, a toast for refused actions,
 * and a guard so links to tickers outside the snapshot explain themselves
 * instead of landing on a 404. Renders nothing in a normal build.
 */
export function DemoBanner() {
    const [notice, setNotice] = useState<string | null>(null)

    useEffect(() => {
        if (!IS_DEMO) return
        let timer: number | undefined
        const onNotice = (e: Event) => {
            setNotice((e as CustomEvent<string>).detail)
            window.clearTimeout(timer)
            timer = window.setTimeout(() => setNotice(null), 4500)
        }
        // Capture on window so this runs before Next's <Link> click handler.
        const onClick = (e: MouseEvent) => {
            const a = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null
            if (!a) return
            const url = new URL(a.href, window.location.href)
            if (url.origin !== window.location.origin) return
            const m = url.pathname.match(TICKER_PATH)
            if (m && !isSnapshotTicker(decodeURIComponent(m[1]))) {
                e.preventDefault()
                e.stopPropagation()
                demoNotice(`${decodeURIComponent(m[1]).toUpperCase()} is not in this snapshot. Detail pages are included for ${DEMO_TICKERS.map((t) => t.ticker).join(", ")}.`)
            }
        }
        window.addEventListener(DEMO_NOTICE_EVENT, onNotice)
        window.addEventListener("click", onClick, true)
        return () => {
            window.removeEventListener(DEMO_NOTICE_EVENT, onNotice)
            window.removeEventListener("click", onClick, true)
            window.clearTimeout(timer)
        }
    }, [])

    if (!IS_DEMO) return null
    return (
        <>
            <div className="border-b border-border bg-[var(--dt-surface)] text-[12px] text-muted-foreground">
                <div className="mx-auto max-w-[1400px] px-4 md:px-6 py-1.5 text-center">
                    Live snapshot of my local app, data as of {DEMO_AS_OF}. Code:{" "}
                    <a href={DEMO_REPO_URL} target="_blank" rel="noopener noreferrer" className="text-[var(--dt-accent)] hover:underline">
                        github.com/DevRobi/sec-dilution-tracker
                    </a>
                </div>
            </div>
            {notice && (
                <div role="status" aria-live="polite"
                    className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] max-w-[min(92vw,560px)] panel px-4 py-2.5 text-[13px] text-[var(--dt-text)] shadow-lg">
                    {notice}
                </div>
            )}
        </>
    )
}
