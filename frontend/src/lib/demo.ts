/**
 * Static demo mode (build with NEXT_DEMO=1, see next.config.ts).
 *
 * The demo is the real frontend served from GitHub Pages with no backend: a
 * fetch shim answers every GET /api/... from a frozen JSON snapshot recorded by
 * scripts/record_demo.py, and every write is refused with a short notice.
 * In a normal build DEMO_MODE is unset, IS_DEMO is false and nothing here runs.
 */

export const IS_DEMO = process.env.DEMO_MODE === "1"
export const DEMO_BASE_PATH = process.env.DEMO_BASE_PATH ?? ""
export const DEMO_AS_OF = process.env.DEMO_AS_OF ?? ""
export const DEMO_REPO_URL = "https://github.com/DevRobi/sec-dilution-tracker"

export interface DemoTicker { ticker: string; title: string }

function parseTickers(): DemoTicker[] {
    try {
        return JSON.parse(process.env.DEMO_TICKERS || "[]") as DemoTicker[]
    } catch {
        return []
    }
}

/** Tickers whose detail pages are in the snapshot (inlined at build time). */
export const DEMO_TICKERS: DemoTicker[] = IS_DEMO ? parseTickers() : []
const DEMO_TICKER_SET = new Set(DEMO_TICKERS.map((t) => t.ticker.toUpperCase()))

export function isSnapshotTicker(ticker: string): boolean {
    return DEMO_TICKER_SET.has(ticker.toUpperCase())
}

/** <Link prefetch> for a ticker page: never prefetch pages the static demo doesn't have. */
export function tickerPrefetch(ticker: string | null | undefined): false | undefined {
    return IS_DEMO && !isSnapshotTicker(ticker ?? "") ? false : undefined
}

// ---- Snapshot file naming. Must match canonical_key()/file_name_for() in
// ---- scripts/record_demo.py.

/** path (without /api) + query sorted by key then value, cache-buster `v` dropped. */
export function canonicalKey(pathname: string, search: string): string {
    let path = pathname.startsWith("/api/") ? pathname.slice(4) : pathname
    path = "/" + path.replace(/^\/+|\/+$/g, "")
    const params = [...new URLSearchParams(search).entries()].filter(([k]) => k !== "v")
    params.sort(([ak, av], [bk, bv]) => (ak < bk ? -1 : ak > bk ? 1 : av < bv ? -1 : av > bv ? 1 : 0))
    return path + (params.length ? "?" + params.map(([k, v]) => `${k}=${v}`).join("&") : "")
}

function fnv1a32(s: string): string {
    let h = 0x811c9dc5
    for (const b of new TextEncoder().encode(s)) {
        h ^= b
        h = Math.imul(h, 0x01000193) >>> 0
    }
    return h.toString(16).padStart(8, "0")
}

export function fileNameFor(key: string): string {
    const slug = key.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80)
    return `${slug}-${fnv1a32(key)}.json`
}

// ---- Notices (shown by <DemoBanner />) ----

export const DEMO_NOTICE_EVENT = "demo:notice"

export function demoNotice(message = "Disabled in this read-only snapshot."): void {
    if (typeof window === "undefined") return
    window.dispatchEvent(new CustomEvent(DEMO_NOTICE_EVENT, { detail: message }))
}

/** True when an axios/fetch error is the shim's "not in this snapshot" 404. */
export function isSnapshotMiss(err: unknown): boolean {
    const data = (err as { response?: { data?: { demo?: string } } })?.response?.data
    return data?.demo === "missing"
}

export const SNAPSHOT_MISS_TEXT = "Not in this snapshot. The live app computes this view on demand; the static copy only has the views recorded on " + (DEMO_AS_OF || "the snapshot date") + "."

// ---- Fetch shim ----

interface SnapshotIndex { files: Record<string, string> }

let indexPromise: Promise<SnapshotIndex> | null = null

function loadIndex(realFetch: typeof fetch): Promise<SnapshotIndex> {
    if (!indexPromise) {
        indexPromise = realFetch(`${DEMO_BASE_PATH}/demo-data/_index.json`)
            .then((r) => (r.ok ? r.json() : { files: {} }))
            .catch(() => ({ files: {} }))
    }
    return indexPromise
}

function jsonResponse(body: unknown, status: number): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
    })
}

const ROW_LIMITS = [10, 20, 50, 100, 200, 500, 1000]

async function answerFromSnapshot(url: URL, realFetch: typeof fetch): Promise<Response> {
    const { files } = await loadIndex(realFetch)
    const key = canonicalKey(url.pathname, url.search)

    let file = files[key]
    let sliceTo: number | null = null

    // A smaller `limit` can be served by slicing a larger recorded list.
    const limit = Number(url.searchParams.get("limit"))
    if (!file && limit > 0) {
        for (const candidate of ROW_LIMITS.filter((n) => n > limit)) {
            const params = new URLSearchParams(url.search)
            params.set("limit", String(candidate))
            const alt = files[canonicalKey(url.pathname, "?" + params.toString())]
            if (alt) { file = alt; sliceTo = limit; break }
        }
    }

    if (!file) {
        return jsonResponse({ detail: "Not in this snapshot", demo: "missing", key }, 404)
    }
    const res = await realFetch(`${DEMO_BASE_PATH}/demo-data/${file}`)
    if (!res.ok) return jsonResponse({ detail: "Not in this snapshot", demo: "missing", key }, 404)
    if (sliceTo === null) return res
    const data = await res.json()
    return jsonResponse(Array.isArray(data) ? data.slice(0, sliceTo) : data, 200)
}

declare global {
    interface Window { __demoFetchShim?: boolean }
}

/** Replace window.fetch once so any /api/... request is answered from the snapshot. */
export function installDemoFetchShim(): void {
    if (!IS_DEMO || typeof window === "undefined" || window.__demoFetchShim) return
    window.__demoFetchShim = true
    const realFetch = window.fetch.bind(window)

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const raw = input instanceof Request ? input.url : String(input)
        const url = new URL(raw, window.location.href)
        if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) {
            return realFetch(input, init)
        }
        const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase()
        if (method !== "GET" && method !== "HEAD") {
            demoNotice("Changes are disabled in this read-only snapshot.")
            return jsonResponse({ detail: "Disabled in the read-only demo snapshot", demo: "readonly" }, 403)
        }
        return answerFromSnapshot(url, realFetch)
    }
}

// Install as soon as this module is evaluated (before any component effect runs).
installDemoFetchShim()
