"use client"

import { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** Flat bordered section with a small-caps title row. */
export function Panel({
    title, meta, actions, children, className, bodyClassName,
}: {
    title?: ReactNode
    meta?: ReactNode
    actions?: ReactNode
    children: ReactNode
    className?: string
    bodyClassName?: string
}) {
    return (
        <section className={cn("panel", className)}>
            {(title || actions) && (
                <header className="flex items-center justify-between gap-4 px-4 min-h-11 py-2 border-b border-border">
                    <div className="flex items-baseline gap-3 min-w-0">
                        {title && <h2 className="label !text-[var(--dt-muted)] truncate">{title}</h2>}
                        {meta && <span className="text-xs text-faint num truncate">{meta}</span>}
                    </div>
                    {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
                </header>
            )}
            <div className={bodyClassName}>{children}</div>
        </section>
    )
}

export function Stat({ label, value, sub, valueClass }: {
    label: string
    value: ReactNode
    sub?: ReactNode
    valueClass?: string
}) {
    return (
        <div className="px-4 py-3 min-w-0 bg-[var(--dt-surface)]">
            <div className="label">{label}</div>
            <div className={cn("mt-1 text-lg font-medium num truncate", valueClass)}>{value}</div>
            {sub && <div className="text-xs text-faint num truncate">{sub}</div>}
        </div>
    )
}

/** Segmented control (period pickers, tabs). */
export function Segmented<T extends string | number>({
    options, value, onChange, size = "sm",
}: {
    options: { value: T; label: string }[]
    value: T
    onChange: (v: T) => void
    size?: "sm" | "md"
}) {
    return (
        <div className="inline-flex rounded-md border border-border p-0.5 bg-[var(--dt-bg)]">
            {options.map((o) => (
                <button
                    key={String(o.value)}
                    type="button"
                    onClick={() => onChange(o.value)}
                    aria-pressed={o.value === value}
                    className={cn(
                        "rounded-[4px] font-medium num transition-colors",
                        size === "sm" ? "h-6 px-2 text-xs" : "h-7 px-3 text-[13px]",
                        o.value === value
                            ? "bg-[var(--dt-hover)] text-foreground"
                            : "text-muted-foreground hover:text-foreground"
                    )}
                >
                    {o.label}
                </button>
            ))}
        </div>
    )
}

/** Quiet outlined tag. Accent for flags/links; down/up only for dilution/buyback meaning. */
export function Tag({ children, tone = "neutral", title, className }: {
    children: ReactNode
    tone?: "neutral" | "accent" | "down" | "up"
    title?: string
    className?: string
}) {
    const tones: Record<string, string> = {
        neutral: "border-border text-muted-foreground",
        accent: "border-[color-mix(in_srgb,var(--dt-accent)_45%,transparent)] text-[var(--dt-accent)]",
        down: "border-[color-mix(in_srgb,var(--dt-down)_45%,transparent)] text-[var(--dt-down)]",
        up: "border-[color-mix(in_srgb,var(--dt-up)_45%,transparent)] text-[var(--dt-up)]",
    }
    return (
        <span title={title} className={cn(
            "inline-flex items-center h-5 px-1.5 rounded-[4px] border text-[11px] font-medium leading-none whitespace-nowrap",
            tones[tone], className)}>
            {children}
        </span>
    )
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
    return (
        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
            <div>
                <h1 className="text-xl font-semibold">{title}</h1>
                {description && <p className="mt-1 text-[13px] text-muted-foreground max-w-2xl">{description}</p>}
            </div>
            {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
    )
}

export function Empty({ children }: { children: ReactNode }) {
    return <div className="px-4 py-8 text-center text-[13px] text-muted-foreground">{children}</div>
}

export type Severity = "error" | "warning" | "info"

export function SeverityDot({ severity }: { severity: Severity }) {
    const color = severity === "error" ? "var(--dt-down)" : severity === "warning" ? "var(--dt-accent)" : "var(--dt-faint)"
    return (
        <span className="inline-flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ background: color }} />
            {severity}
        </span>
    )
}

export function FilingLink({ url, label }: { url?: string | null; label: string }) {
    const text = label.split(":")[0] || label
    if (!url) return <span className="text-xs text-muted-foreground">{text}</span>
    return (
        <a href={url} target="_blank" rel="noopener noreferrer"
            className="text-xs text-[var(--dt-accent)] hover:underline whitespace-nowrap">
            {text}
        </a>
    )
}
