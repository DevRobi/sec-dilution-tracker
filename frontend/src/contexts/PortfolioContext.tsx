"use client"

import { createContext, useContext, useState, ReactNode, useEffect, useCallback, useRef } from "react"
import api from "@/lib/api"
import { DilutionAlert } from "@/components/DilutionAlerts"
import { DEMO_TICKERS, IS_DEMO } from "@/lib/demo"

// Cache TTL constants (in milliseconds)
const CACHE_TTL = {
    PORTFOLIO: 5 * 60 * 1000,      // 5 minutes for portfolio list
    DETAILS: 60 * 60 * 1000,       // 1 hour for ticker details
    ALERTS: 5 * 60 * 1000,         // 5 minutes for alerts
}

// v2: share counts are split-adjusted; drop details cached before that change.
const STORAGE_KEY = 'shareTracker_cache_v2'

export interface PortfolioItem {
    cik: string
    ticker: string
    title: string
    stats: {
        latest_shares: number
        change_pct_1q: number
        change_pct_1y: number
        change_pct_5y: number
        dilution_summary?: string
    }
}


export interface TickerDetails {
    history: any[]
    priceData: any[]
    capData: any[]
    offerings: any[]
    lastUpdated: number
}

interface CachedData {
    items: PortfolioItem[]
    alerts: DilutionAlert[]
    detailsCache: Record<string, TickerDetails>
    portfolioLastUpdated: number
    alertsLastUpdated: number
}

interface PortfolioContextType {
    items: PortfolioItem[]
    alerts: DilutionAlert[]
    loading: boolean
    refreshPortfolio: (force?: boolean) => Promise<void>
    detailsCache: Record<string, TickerDetails>
    cacheTickerDetails: (ticker: string, data: Partial<TickerDetails>) => void
    addItemOptimistic: (item: PortfolioItem) => void
    removeItemOptimistic: (ticker: string) => void
}

const PortfolioContext = createContext<PortfolioContextType | undefined>(undefined)

// Helper to load from localStorage
function loadFromStorage(): CachedData | null {
    if (typeof window === 'undefined') return null
    try {
        const stored = localStorage.getItem(STORAGE_KEY)
        if (stored) {
            return JSON.parse(stored)
        }
    } catch (e) {
        console.warn('Failed to load cache from localStorage:', e)
    }
    return null
}

// Helper to save to localStorage
function saveToStorage(data: CachedData) {
    if (typeof window === 'undefined') return
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
    } catch (e) {
        console.warn('Failed to save cache to localStorage:', e)
    }
}

export function PortfolioProvider({ children }: { children: ReactNode }) {
    const [items, setItems] = useState<PortfolioItem[]>([])
    const [alerts, setAlerts] = useState<DilutionAlert[]>([])
    const [detailsCache, setDetailsCache] = useState<Record<string, TickerDetails>>({})
    const [loading, setLoading] = useState(true)
    const [loaded, setLoaded] = useState(false)
    const portfolioLastUpdated = useRef<number>(0)
    const alertsLastUpdated = useRef<number>(0)
    const isRevalidating = useRef<boolean>(false)

    // Load from localStorage on mount (stale data for instant display)
    useEffect(() => {
        const cached = loadFromStorage()
        if (cached) {
            if (cached.items?.length > 0) setItems(cached.items)
            if (cached.alerts?.length > 0) setAlerts(cached.alerts)
            if (cached.detailsCache) setDetailsCache(cached.detailsCache)
            // Intentionally do NOT restore the persisted freshness timestamps.
            // Keeping them at 0 forces a backend revalidation on every fresh page
            // load (stale-while-revalidate): the cached list shows instantly, but
            // the server is always reconciled. Restoring the timestamps let a
            // removed ticker reappear after a reload because the 5-min freshness
            // gate would short-circuit the refetch.
            portfolioLastUpdated.current = 0
            alertsLastUpdated.current = 0
            setLoading(false) // Show stale data immediately
        }
    }, [])

    // Save to localStorage when data changes
    useEffect(() => {
        if (items.length > 0 || Object.keys(detailsCache).length > 0) {
            saveToStorage({
                items,
                alerts,
                detailsCache,
                portfolioLastUpdated: portfolioLastUpdated.current,
                alertsLastUpdated: alertsLastUpdated.current
            })
        }
    }, [items, alerts, detailsCache])

    const cacheTickerDetails = useCallback((ticker: string, data: Partial<TickerDetails>) => {
        setDetailsCache(prev => ({
            ...prev,
            [ticker.toUpperCase()]: {
                ...prev[ticker.toUpperCase()],
                ...data,
                lastUpdated: Date.now()
            }
        }))
    }, [])

    const addItemOptimistic = useCallback((item: PortfolioItem) => {
        setItems(prev => {
            const exists = prev.some(p => p.ticker === item.ticker);
            if (exists) return prev;
            return [item, ...prev];
        });
    }, []);

    const removeItemOptimistic = useCallback((ticker: string) => {
        const uTicker = ticker.toUpperCase();
        setItems(prev => prev.filter(p => p.ticker.toUpperCase() !== uTicker));
        // Alerts are derived from portfolio membership, so a removed ticker must
        // also disappear from the alerts list (and from the persisted cache).
        setAlerts(prev => prev.filter(a => (a.ticker || "").toUpperCase() !== uTicker));
        // Drop the cached ticker details (offerings/history/price) too, so a
        // re-add re-fetches fresh data and a stale detail view can't reappear
        // from localStorage. The [items, alerts, detailsCache] persist effect
        // writes the pruned cache back to localStorage.
        setDetailsCache(prev => {
            if (!(uTicker in prev)) return prev;
            const next = { ...prev };
            delete next[uTicker];
            return next;
        });
    }, []);

    // Background prefetch ticker details (non-blocking)
    const prefetchTickerDetails = useCallback(async (tickers: string[]) => {
        const now = Date.now()
        const tickersToFetch = tickers.filter(t => {
            const cached = detailsCache[t.toUpperCase()]
            if (!cached) return true
            return now - cached.lastUpdated > CACHE_TTL.DETAILS
        }).slice(0, 3)

        if (tickersToFetch.length === 0) return

        tickersToFetch.forEach(ticker => {
            Promise.allSettled([
                api.get(`/tickers/${ticker}/history`),
                api.get(`/tickers/${ticker}/price`),
                api.get(`/tickers/${ticker}/offerings`)
            ]).then(([historyRes, priceRes, offeringsRes]) => {
                const update: Partial<TickerDetails> = {}
                if (historyRes.status === 'fulfilled') update.history = historyRes.value.data
                if (priceRes.status === 'fulfilled') update.priceData = priceRes.value.data
                if (offeringsRes.status === 'fulfilled') update.offerings = offeringsRes.value.data

                if (Object.keys(update).length > 0) {
                    cacheTickerDetails(ticker, update)
                }
            })
        })
    }, [detailsCache, cacheTickerDetails])

    const refreshPortfolio = useCallback(async (force: boolean = false) => {
        const now = Date.now()
        const portfolioFresh = now - portfolioLastUpdated.current < CACHE_TTL.PORTFOLIO

        if (portfolioFresh && !force && items.length > 0) {
            setLoading(false)
            return
        }

        // Demo build: a read-only "demo user" whose portfolio is the snapshot's
        // tickers, read from the snapshot (no personal portfolio is recorded).
        if (IS_DEMO) {
            if (isRevalidating.current) return
            isRevalidating.current = true
            if (items.length === 0) setLoading(true)
            try {
                const rows = await Promise.all(DEMO_TICKERS.map(async (t): Promise<PortfolioItem | null> => {
                    const [info, stats] = await Promise.allSettled([
                        api.get(`/tickers/${t.ticker}`),
                        api.get(`/tickers/${t.ticker}/stats`),
                    ])
                    if (info.status !== "fulfilled") return null
                    return {
                        ...info.value.data,
                        stats: stats.status === "fulfilled" ? stats.value.data : {},
                    } as PortfolioItem
                }))
                setItems(rows.filter((r): r is PortfolioItem => r !== null))
                setAlerts([])
                portfolioLastUpdated.current = now
                setLoaded(true)
            } finally {
                setLoading(false)
                isRevalidating.current = false
            }
            return
        }

        // Signed-out visitors (rankings, ticker pages) have no portfolio: skip the
        // request instead of producing a 401 on every page load.
        if (typeof window !== "undefined" && !window.localStorage.getItem("token")) {
            setLoading(false)
            return
        }

        if (isRevalidating.current) return
        isRevalidating.current = true

        if (items.length === 0) setLoading(true)

        try {
            const [portfolioRes, alertsRes] = await Promise.allSettled([
                api.get("/portfolio/full"),
                api.get("/portfolio/alerts")
            ])

            if (portfolioRes.status === 'fulfilled' && Array.isArray(portfolioRes.value.data)) {
                const newData = portfolioRes.value.data;
                const uniqueItems = Array.from(
                    new Map(newData.map((item: any) => [item.cik, item])).values()
                ) as PortfolioItem[];

                if (newData.length === 0 && items.length > 0 && !force) {
                    console.warn("Portfolio revalidation returned empty list - ignoring to prevent state loss");
                } else {
                    setItems(uniqueItems)
                    portfolioLastUpdated.current = now

                    saveToStorage({
                        items: uniqueItems,
                        alerts,
                        detailsCache,
                        portfolioLastUpdated: now,
                        alertsLastUpdated: alertsLastUpdated.current
                    })
                }

                const tickersToPreload = uniqueItems.slice(0, 3).map((item: PortfolioItem) => item.ticker)
                prefetchTickerDetails(tickersToPreload)
            } else {
                if (portfolioRes.status === 'rejected') {
                    const reason = (portfolioRes as PromiseRejectedResult).reason
                    // 401 just means "not logged in" (public rankings/ticker pages) —
                    // not an error worth surfacing in the dev overlay.
                    if (reason?.response?.status === 401) {
                        console.info("Portfolio not loaded: not signed in")
                    } else {
                        console.error("Portfolio fetch failed:", reason)
                    }
                }
            }

            if (alertsRes.status === 'fulfilled') {
                setAlerts(alertsRes.value.data)
                alertsLastUpdated.current = now
            }

            setLoaded(true)
        } catch (e) {
            console.error(e)
        } finally {
            setLoading(false)
            isRevalidating.current = false
        }
    }, [items, alerts, detailsCache, prefetchTickerDetails])

    useEffect(() => {
        if (!loaded) {
            refreshPortfolio()
        }
    }, [loaded, refreshPortfolio])

    return (
        <PortfolioContext.Provider
            value={{
                items,
                alerts,
                loading,
                refreshPortfolio,
                detailsCache,
                cacheTickerDetails,
                addItemOptimistic,
                removeItemOptimistic
            }}
        >
            {children}
        </PortfolioContext.Provider>
    )
}

export function usePortfolio() {
    const context = useContext(PortfolioContext)
    if (context === undefined) {
        throw new Error("usePortfolio must be used within a PortfolioProvider")
    }
    return context
}
