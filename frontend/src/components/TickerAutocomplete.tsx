"use client"

import * as React from "react"
import { Search, Loader2 } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import {
    Command,
    CommandGroup,
    CommandItem,
    CommandList,
} from "@/components/ui/command"
import {
    Popover,
    PopoverContent,
    PopoverAnchor,
} from "@/components/ui/popover"
import { useDebounce } from "@/hooks/use-debounce"
import { getApiBaseCandidates } from "@/lib/api"
import { DEMO_TICKERS, IS_DEMO } from "@/lib/demo"

interface TickerAutocompleteProps {
    onSelect: (ticker: string) => void
    placeholder?: string
    className?: string
    compact?: boolean
}

export function TickerAutocomplete({ onSelect, placeholder = "Search ticker", className, compact = false }: TickerAutocompleteProps) {
    const [open, setOpen] = React.useState(false)
    const [query, setQuery] = React.useState("")
    const [results, setResults] = React.useState<any[]>([])
    const [loading, setLoading] = React.useState(false)
    const containerRef = React.useRef<HTMLDivElement>(null)
    
    // Track which query the current results are for (prevents race conditions)
    const latestQueryRef = React.useRef<string>("")
    const requestIdRef = React.useRef<number>(0)

    const debouncedQuery = useDebounce(query, 150)  // Slightly longer debounce to reduce race conditions

    React.useEffect(() => {
        // Skip empty or single-character queries
        if (!debouncedQuery || debouncedQuery.length < 2) {
            setResults([])
            setLoading(false)
            latestQueryRef.current = ""
            requestIdRef.current += 1
            return
        }

        // Increment request ID - only the latest request will update results
        const thisRequestId = ++requestIdRef.current
        latestQueryRef.current = debouncedQuery

        // Demo snapshot: search only the tickers that have recorded detail pages.
        if (IS_DEMO) {
            const q = debouncedQuery.trim().toLowerCase()
            setResults(DEMO_TICKERS.filter((t) =>
                t.ticker.toLowerCase().startsWith(q) || t.title.toLowerCase().includes(q)))
            setLoading(false)
            return
        }
        
        const controller = new AbortController()

        const doSearch = async () => {
            setLoading(true)
            try {
                const apiBases = getApiBaseCandidates()
                let gotResponse = false
                let lastError: unknown = null

                for (const apiBase of apiBases) {
                    let timeoutId: number | undefined
                    try {
                        timeoutId = window.setTimeout(() => controller.abort(), 8000)
                        const response = await fetch(`${apiBase}/tickers/search?q=${encodeURIComponent(debouncedQuery)}`, {
                            method: "GET",
                            signal: controller.signal,
                        })
                        if (!response.ok) {
                            continue
                        }

                        const data = await response.json()
                        gotResponse = true

                        // CRITICAL: Only update if this is still the latest request AND query matches
                        if (thisRequestId === requestIdRef.current && latestQueryRef.current === debouncedQuery) {
                            setResults(data)
                        }
                        break
                    } catch (err) {
                        lastError = err
                    } finally {
                        if (timeoutId !== undefined) {
                            window.clearTimeout(timeoutId)
                        }
                    }
                }

                if (!gotResponse && lastError) {
                    throw lastError
                }
                if (!gotResponse) {
                    throw new Error("Search endpoint unavailable")
                }
            } catch (e: any) {
                if (e.name !== "AbortError") {
                    console.error(e)
                }
            } finally {
                // Only clear loading if this is the latest request
                if (thisRequestId === requestIdRef.current) {
                    setLoading(false)
                }
            }
        }
        doSearch()

        return () => {
            controller.abort()
        }
    }, [debouncedQuery])

    // Close popover when clicking outside
    React.useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
                setOpen(false)
            }
        }
        document.addEventListener("mousedown", handleClickOutside)
        return () => document.removeEventListener("mousedown", handleClickOutside)
    }, [])

    return (
        <div ref={containerRef} className="relative w-full">
            <div className="relative flex items-center">
                <Input
                    placeholder={placeholder}
                    value={query}
                    onChange={(e) => {
                        setQuery(e.target.value)
                        setOpen(true)
                    }}
                    onKeyDown={(e) => {
                        // Enter picks the exact ticker match (or the top result); with no
                        // results yet, a ticker-shaped query is used as typed.
                        if (e.key !== "Enter") return
                        const q = query.trim().toUpperCase()
                        const pick = results.find((r) => String(r.ticker).toUpperCase() === q)?.ticker
                            ?? results[0]?.ticker
                            ?? (!IS_DEMO && /^[A-Z.\-]{1,6}$/.test(q) ? q : null)
                        if (pick) {
                            e.preventDefault()
                            onSelect(String(pick).toUpperCase())
                            setQuery("")
                            setResults([])
                            setOpen(false)
                        }
                    }}
                    onFocus={() => setOpen(true)}
                    className={cn(
                        "bg-[var(--dt-bg)] border-border shadow-none focus-visible:ring-0 focus-visible:border-[var(--dt-accent)]",
                        compact ? "h-8 text-[13px] pl-8" : "h-10 text-sm pl-9",
                        className
                    )}
                />
                <Search className={cn("absolute text-faint", compact ? "left-2.5 w-3.5 h-3.5" : "left-3 w-4 h-4")} />
                {loading && (
                    <Loader2 className={cn("absolute text-faint animate-spin", compact ? "right-2.5 w-3.5 h-3.5" : "right-3 w-4 h-4")} />
                )}
            </div>

            <Popover open={open && (results.length > 0 || !!(query && !loading))}>
                <PopoverAnchor asChild>
                    <div className="absolute top-full w-full pointer-events-none" />
                </PopoverAnchor>
                <PopoverContent
                    className="w-[var(--radix-popover-trigger-width)] min-w-64 p-1 border-border shadow-none"
                    onOpenAutoFocus={(e) => e.preventDefault()}
                >
                    <Command>
                        <CommandList>
                            {results.length === 0 && query && !loading && (
                                <div className="py-4 text-center text-[13px] text-muted-foreground">No ticker found.</div>
                            )}
                            <CommandGroup>
                                {results.map((item) => (
                                    <CommandItem
                                        key={item.ticker}
                                        value={item.ticker}
                                        onSelect={() => {
                                            setQuery("")
                                            setResults([])
                                            setLoading(false)
                                            onSelect(item.ticker)
                                            setOpen(false)
                                        }}
                                        className="cursor-pointer px-3 py-2 rounded-[4px]"
                                    >
                                        <div className="flex items-baseline gap-3 min-w-0">
                                            <span className="w-14 shrink-0 font-medium text-[13px]">{item.ticker}</span>
                                            <span className="text-xs text-muted-foreground truncate">{item.title}</span>
                                        </div>
                                    </CommandItem>
                                ))}
                            </CommandGroup>
                        </CommandList>
                    </Command>
                </PopoverContent>
            </Popover>
        </div>
    )
}
