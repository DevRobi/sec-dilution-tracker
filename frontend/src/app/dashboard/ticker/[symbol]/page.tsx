"use client"

import { useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import api from "@/lib/api"
import { TickerDetailView } from "@/components/TickerDetailView"
import { usePortfolio } from "@/contexts/PortfolioContext"
import { IS_DEMO, demoNotice } from "@/lib/demo"

export default function TickerPage() {
    const paramsHook = useParams()
    const symbol = (paramsHook.symbol as string).toUpperCase()

    const [data, setData] = useState<any>(null)
    const [history, setHistory] = useState<any[]>([])
    const [priceData, setPriceData] = useState<any[]>([])
    const [capData, setCapData] = useState<any[]>([])
    const [offerings, setOfferings] = useState<any[]>([])
    const [loading, setLoading] = useState(true)
    const [chartsLoading, setChartsLoading] = useState(true)
    const [added, setAdded] = useState(false)
    const { items, detailsCache, cacheTickerDetails } = usePortfolio()
    const requestVersionRef = useRef(0)

    // Hydrate from cache immediately (Basic and Detailed)
    useEffect(() => {
        // Hydrate basic info from portfolio items
        if (!data && items.length > 0) {
            const cached = items.find(i => i.ticker === symbol)
            if (cached) {
                setData({ ...cached, ticker: cached.ticker, title: cached.title, stats: cached.stats })
                setLoading(false)
            }
        }
        setAdded(items.some(i => i.ticker.toUpperCase() === symbol))

        // Hydrate details from chart cache
        const cachedDetails = detailsCache[symbol]
        if (cachedDetails) {
            // Check if cache is fresh enough (e.g., 24h)
            if (Date.now() - cachedDetails.lastUpdated < 1000 * 60 * 60 * 24) {
                // Only use cached data if arrays have actual content (not empty)
                if (cachedDetails.history?.length > 0) setHistory(cachedDetails.history)
                if (cachedDetails.priceData?.length > 0) setPriceData(cachedDetails.priceData)
                if (cachedDetails.capData?.length > 0) setCapData(cachedDetails.capData)
                if (cachedDetails.offerings?.length > 0) setOfferings(cachedDetails.offerings)

                // Only skip loading if we have actual data
                if (cachedDetails.history?.length > 0 && cachedDetails.priceData?.length > 0 && cachedDetails.capData?.length > 0) {
                    setChartsLoading(false)
                }
            }
        }
    }, [symbol, items, detailsCache])

    const fetchData = async () => {
        const requestVersion = ++requestVersionRef.current
        const isStale = () => requestVersion !== requestVersionRef.current

        // Only show full page loader if we don't have basic data yet
        if (!data) setLoading(true)

        // Only show chart loaders if we didn't just hydrate valid cache
        if (!detailsCache[symbol]) setChartsLoading(true)

        try {
            // 1. Get Basic Info first (Fastest) to show title/layout
            let comp: any;
            try {
                // We'll reuse existing logic basically but save lines here for the edit
                comp = await api.get(`/tickers/${symbol}`)
                if (isStale()) return
                setData((prev: any) => ({ ...prev, ...comp.data }))
                setLoading(false) // Basic info loaded
            } catch (e: any) {
                if (e.response?.status === 404) {
                    setData(null)
                    setLoading(false)
                    setChartsLoading(false)
                    return
                } else {
                    throw e
                }
            }

            // 2. Fetch everything else in parallel (Non-blocking)

            const pStats = api.get(`/tickers/${symbol}/stats`)
                .then(res => {
                    if (!isStale()) {
                        setData((prev: any) => ({ ...prev, stats: res.data }))
                    }
                })
                .catch(() => { })

            const pHistory = api.get(`/tickers/${symbol}/history`)
                .then(res => {
                    if (isStale()) return
                    setHistory(res.data)
                    cacheTickerDetails(symbol, { history: res.data })
                })
                .catch(() => { })

            const pPrice = api.get(`/tickers/${symbol}/price`)
                .then(res => {
                    if (isStale()) return
                    setPriceData(res.data)
                    cacheTickerDetails(symbol, { priceData: res.data })
                })
                .catch(() => { })

            const pCap = api.get(`/tickers/${symbol}/market_cap`)
                .then(res => {
                    if (isStale()) return
                    setCapData(res.data)
                    cacheTickerDetails(symbol, { capData: res.data })
                })
                .catch(() => {
                    if (!isStale()) {
                        cacheTickerDetails(symbol, { capData: [] })
                    }
                })

            // Offerings are DB-backed. The daily worker refreshes data; the UI no
            // longer polls while waiting for request-time syncs.
            const fetchOfferings = async (): Promise<void> => {
                try {
                    if (isStale()) return
                    const res = await api.get(`/tickers/${symbol}/offerings`)
                    if (isStale()) return
                    setOfferings(res.data || [])
                    cacheTickerDetails(symbol, { offerings: res.data || [] })
                } catch {
                    if (!isStale()) {
                        setOfferings([])
                    }
                }
            }
            const pOff = fetchOfferings()

            // Wait for charts to finish before hiding skeleton
            Promise.allSettled([pStats, pHistory, pPrice, pCap, pOff]).finally(() => {
                if (!isStale()) {
                    setChartsLoading(false)
                }
            })

        } catch (e) {
            if (!isStale()) {
                console.error(e)
                setLoading(false)
                setChartsLoading(false)
            }
        }
    }

    const handleRefreshOfferings = async () => {
        if (IS_DEMO) return demoNotice("Reload is disabled: this is a frozen snapshot.")
        try {
            const res = await api.get(`/tickers/${symbol}/offerings`)
            if (res.data) {
                setOfferings(res.data)
                cacheTickerDetails(symbol, { offerings: res.data })
            }
        } catch (e) {
            console.error(`Failed to refresh offerings for ${symbol}:`, e)
        }
    }

    const handleSync = async () => {
        if (IS_DEMO) return demoNotice("Refresh is disabled: this is a frozen snapshot.")
        // User-facing refresh is now database-only. External SEC/Polygon refresh
        // runs in the scheduled non-AI worker.
        fetchData()
    }

    const handleAddToPortfolio = async () => {
        if (IS_DEMO) return demoNotice("Portfolio edits are disabled in this read-only snapshot.")
        await api.post(`/portfolio/${symbol}`)
        setAdded(true)
    }

    useEffect(() => {
        if (symbol) {
            fetchData()
        }
        return () => {
            requestVersionRef.current += 1
        }
    }, [symbol])

    if (loading && !data) {
        return (
            <div className="space-y-4">
                <div className="skeleton h-8 w-48" />
                <div className="skeleton h-16 w-full" />
                <div className="skeleton h-[360px] w-full" />
            </div>
        )
    }
    if (!data) {
        return (
            <div className="panel px-4 py-10 text-center text-[13px] text-muted-foreground">
                <span className="text-foreground font-medium">{symbol}</span>{IS_DEMO
                    ? " is not in this snapshot."
                    : <> is not in the tracked universe yet. It will appear after the daily data job loads it.</>}
            </div>
        )
    }

    return (
        <TickerDetailView
            tickerData={data}
            history={history}
            priceData={priceData}
            capData={capData}
            offerings={offerings}
            isAdded={added}
            onAdd={handleAddToPortfolio}
            loading={chartsLoading}
            onRefreshOfferings={handleRefreshOfferings}
            onSync={handleSync}
        />
    )
}
