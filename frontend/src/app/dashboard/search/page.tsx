"use client"

import api from "@/lib/api"
import { PageHeader } from "@/components/terminal/primitives"
import { TickerAutocomplete } from "@/components/TickerAutocomplete"
import { TickerDetailView } from "@/components/TickerDetailView"
import { useSearch } from "@/contexts/SearchContext"
import { useRef } from "react"
import { usePortfolio } from "@/contexts/PortfolioContext"
import { IS_DEMO, demoNotice } from "@/lib/demo"

export default function SearchPage() {
    const { refreshPortfolio, addItemOptimistic, items } = usePortfolio()
    const searchRequestId = useRef(0)
    const {
        searchState,
        setSearchQuery,
        setSearchResult,
        setSearchLoading,
        setSearchError,
        setSearchHistory,
        setSearchPriceData,
        setSearchCapData,
        setSearchOfferings,
        setSearchAdded
    } = useSearch()

    const handleSearchStr = async (inputQuery: string) => {
        if (!inputQuery) return
        const currentRequestId = ++searchRequestId.current
        const isStale = () => currentRequestId !== searchRequestId.current

        setSearchLoading(true)
        setSearchError("")
        setSearchResult(null)
        setSearchQuery(inputQuery)
        setSearchHistory([])
        setSearchPriceData([])
        setSearchCapData([])
        setSearchOfferings([])
        setSearchAdded(false)

        const q = inputQuery

        try {
            const res = await api.get(`/tickers/${q}`)
            if (isStale()) return

            // Fetch all data in parallel for faster detail load.
            const [stats, hist, prices, caps, offerings] = await Promise.all([
                api.get(`/tickers/${q}/stats`).catch(() => ({ data: {} })),
                api.get(`/tickers/${q}/history`).catch(() => ({ data: [] })),
                api.get(`/tickers/${q}/price`).catch(() => ({ data: [] })),
                api.get(`/tickers/${q}/market_cap`).catch(() => ({ data: [] })),
                api.get(`/tickers/${q}/offerings`).then(res => res.data || []).catch(() => []),
            ])
            if (isStale()) return

            setSearchResult({ ...res.data, stats: stats.data })
            setSearchHistory(hist.data)
            setSearchPriceData(prices.data)
            setSearchCapData(caps.data)
            setSearchOfferings(offerings)
            setSearchAdded(items.some((item: any) => item.ticker.toUpperCase() === q.toUpperCase()))

        } catch (err: any) {
            if (!isStale()) {
                if (IS_DEMO) {
                    setSearchError(`${q} is not in this snapshot.`)
                } else if (err.response?.status === 404) {
                    setSearchError("This ticker is not in the tracked universe yet. It will appear after the daily data job loads it.")
                } else {
                    setSearchError("Ticker data is unavailable right now.")
                }
            }
        } finally {
            if (!isStale()) {
                setSearchLoading(false)
            }
        }
    }

    const handleAdd = async () => {
        if (!searchState.result) return;
        if (IS_DEMO) return demoNotice("Portfolio edits are disabled in this read-only snapshot.");
        const ticker = searchState.result.ticker;

        // Optimistic local state (even though search context is separate, 
        // we can trigger portfolio context if we want, but usually search just adds)
        setSearchAdded(true);

        try {
            // OPTIMISTIC ADD: We have the full data in search results
            if (searchState.result) {
                addItemOptimistic({
                    cik: searchState.result.cik,
                    ticker: searchState.result.ticker,
                    title: searchState.result.title,
                    stats: searchState.result.stats || {}
                })
            }

            await api.post(`/portfolio/${ticker}`)
            // Refresh portfolio list in background for stats sync
            refreshPortfolio(false);
        } catch (e) {
            setSearchAdded(false);
            console.error("Failed to add to portfolio", e);
        }
    }

    return (
        <div className="space-y-4">
            <PageHeader title="Search" description="Look up any company in the tracked universe by ticker or name." />
            <div className="max-w-xl">
                <TickerAutocomplete
                    placeholder="Ticker or company name, e.g. AAPL"
                    onSelect={(val) => {
                        handleSearchStr(val)
                    }}
                />
            </div>

            {searchState.loading && (
                <div className="space-y-4">
                    <div className="skeleton h-8 w-48" />
                    <div className="skeleton h-16 w-full" />
                    <div className="skeleton h-[320px] w-full" />
                </div>
            )}

            {searchState.error && (
                <div className="panel px-4 py-6 text-center text-[13px] text-muted-foreground">{searchState.error}</div>
            )}

            {/* Full Ticker Details */}
            {searchState.result && !searchState.loading && (
                <TickerDetailView
                    tickerData={searchState.result}
                    history={searchState.history}
                    priceData={searchState.priceData}
                    capData={searchState.capData}
                    offerings={searchState.offerings}
                    isAdded={searchState.added}
                    onAdd={handleAdd}
                    loading={searchState.chartsLoading}
                    onRefreshOfferings={async () => {
                        if (IS_DEMO) return demoNotice("Reload is disabled: this is a frozen snapshot.")
                        const ticker = searchState.result?.ticker
                        if (!ticker) return
                        try {
                            const [offeringsRes, statsRes] = await Promise.all([
                                api.get(`/tickers/${ticker}/offerings`),
                                api.get(`/tickers/${ticker}/stats`).catch(() => null)
                            ])
                            setSearchOfferings(offeringsRes.data || [])
                            if (statsRes?.data) {
                                setSearchResult({ ...searchState.result, stats: statsRes.data })
                            }
                        } catch (e) {
                            console.error('Failed to trigger refresh:', e)
                            setSearchError("Failed to refresh cached data.")
                        } finally {
                            setSearchLoading(false)
                        }
                    }}
                />
            )}

            {!searchState.result && !searchState.loading && !searchState.error && (
                <p className="text-[13px] text-faint">Results show share-count history, recorded offerings and audit flags for the company.</p>
            )}
        </div>
    )
}
