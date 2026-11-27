"use client"

import { createContext, useContext, useState, ReactNode } from "react"

interface SearchState {
    query: string
    result: any | null
    loading: boolean
    error: string
    history: any[]
    priceData: any[]
    capData: any[]
    offerings: any[]
    added: boolean
    chartsLoading: boolean
}

interface SearchContextType {
    searchState: SearchState
    setSearchQuery: (query: string) => void
    setSearchResult: (result: any) => void
    setSearchLoading: (loading: boolean) => void
    setSearchError: (error: string) => void
    setSearchHistory: (history: any[]) => void
    setSearchPriceData: (data: any[]) => void
    setSearchCapData: (data: any[]) => void
    setSearchOfferings: (data: any[]) => void
    setSearchAdded: (added: boolean) => void
    setChartsLoading: (loading: boolean) => void
    clearSearch: () => void
}

const SearchContext = createContext<SearchContextType | undefined>(undefined)

export function SearchProvider({ children }: { children: ReactNode }) {
    const [searchState, setSearchState] = useState<SearchState>({
        query: "",
        result: null,
        loading: false,
        error: "",
        history: [],
        priceData: [],
        capData: [],
        offerings: [],
        added: false,
        chartsLoading: false
    })

    const setSearchQuery = (query: string) => {
        setSearchState(prev => ({ ...prev, query }))
    }

    const setSearchResult = (result: any) => {
        setSearchState(prev => ({ ...prev, result }))
    }

    const setSearchLoading = (loading: boolean) => {
        setSearchState(prev => ({ ...prev, loading }))
    }

    const setSearchError = (error: string) => {
        setSearchState(prev => ({ ...prev, error }))
    }

    const setSearchHistory = (history: any[]) => {
        setSearchState(prev => ({ ...prev, history }))
    }

    const setSearchPriceData = (data: any[]) => {
        setSearchState(prev => ({ ...prev, priceData: data }))
    }

    const setSearchCapData = (data: any[]) => {
        setSearchState(prev => ({ ...prev, capData: data }))
    }

    const setSearchOfferings = (data: any[]) => {
        setSearchState(prev => ({ ...prev, offerings: data }))
    }

    const setSearchAdded = (added: boolean) => {
        setSearchState(prev => ({ ...prev, added }))
    }

    const setChartsLoading = (loading: boolean) => {
        setSearchState(prev => ({ ...prev, chartsLoading: loading }))
    }

    const clearSearch = () => {
        setSearchState({
            query: "",
            result: null,
            loading: false,
            error: "",
            history: [],
            priceData: [],
            capData: [],
            offerings: [],
            added: false,
            chartsLoading: false
        })
    }

    return (
        <SearchContext.Provider
            value={{
                searchState,
                setSearchQuery,
                setSearchResult,
                setSearchLoading,
                setSearchError,
                setSearchHistory,
                setSearchPriceData,
                setSearchCapData,
                setSearchOfferings,
                setSearchAdded,
                setChartsLoading,
                clearSearch
            }}
        >
            {children}
        </SearchContext.Provider>
    )
}

export function useSearch() {
    const context = useContext(SearchContext)
    if (context === undefined) {
        throw new Error("useSearch must be used within a SearchProvider")
    }
    return context
}
