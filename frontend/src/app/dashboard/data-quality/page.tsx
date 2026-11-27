'use client'

import { Fragment, useEffect, useState } from 'react'
import Link from 'next/link'
import api from '@/lib/api'
import { Empty, PageHeader, Panel, Segmented, SeverityDot, type Severity } from '@/components/terminal/primitives'
import { DEMO_BASE_PATH, IS_DEMO, tickerPrefetch } from '@/lib/demo'

const REPORT_HREF = IS_DEMO ? `${DEMO_BASE_PATH}/demo-data/data-quality-report.html` : '/api/data-quality/report'

interface CheckInfo { severity: Severity; description: string; count: number }
interface Sample { ticker: string | null; cik: string | null; message: string }
interface Summary {
    meta: { db: string; as_of: string; generated_at: string; duration_s: number; row_counts: Record<string, number> }
    by_severity: Record<Severity, number>
    by_check: Record<string, CheckInfo>
    total_findings: number
    samples: Record<string, Sample[]>
}

const SEV: Severity[] = ['error', 'warning', 'info']

export default function DataQualityPage() {
    const [summary, setSummary] = useState<Summary | null>(null)
    const [error, setError] = useState<string | null>(null)
    const [open, setOpen] = useState<string | null>(null)
    const [filter, setFilter] = useState<'all' | Severity>('all')

    useEffect(() => {
        api.get('/data-quality/summary')
            .then((res) => setSummary(res.data))
            .catch((err) => setError(err?.response?.data?.detail ?? 'Could not load the data-quality report.'))
    }, [])

    if (error) {
        return (
            <div className="space-y-4">
                <PageHeader title="Data quality" />
                <Panel>
                    <Empty>
                        {error}
                        <code className="block mt-2 text-xs text-faint">cd backend &amp;&amp; venv/bin/python -m app.data_quality audit</code>
                    </Empty>
                </Panel>
            </div>
        )
    }
    if (!summary) {
        return (
            <div className="space-y-4">
                <div className="skeleton h-8 w-56" />
                <div className="skeleton h-20 w-full" />
                <div className="skeleton h-[400px] w-full" />
            </div>
        )
    }

    const checks = Object.entries(summary.by_check)
        .filter(([, c]) => filter === 'all' || c.severity === filter)
        .sort(([an, a], [bn, b]) =>
            (a.count === 0 ? 1 : 0) - (b.count === 0 ? 1 : 0) ||
            SEV.indexOf(a.severity) - SEV.indexOf(b.severity) ||
            b.count - a.count || an.localeCompare(bn))
    const active = Object.values(summary.by_check).filter((c) => c.count > 0).length
    const m = summary.meta

    return (
        <div className="space-y-4">
            <PageHeader
                title="Data quality"
                description="Did the feed arrive, is it complete, and do the numbers add up? Results of the latest audit of the database; click a check to see examples."
                actions={<a className="text-[13px] text-[var(--dt-accent)] hover:underline" href={REPORT_HREF} target="_blank" rel="noreferrer">Open HTML report</a>}
            />

            <div className="panel overflow-hidden grid grid-cols-2 md:grid-cols-5 gap-px !bg-[var(--dt-border)]">
                {SEV.map((s) => (
                    <div key={s} className="bg-[var(--dt-surface)] px-4 py-3">
                        <SeverityDot severity={s} />
                        <div className="mt-1 text-lg font-medium num">{(summary.by_severity[s] ?? 0).toLocaleString()}</div>
                    </div>
                ))}
                <div className="bg-[var(--dt-surface)] px-4 py-3">
                    <div className="label">Checks with findings</div>
                    <div className="mt-1 text-lg font-medium num">{active} <span className="text-faint text-sm">of {Object.keys(summary.by_check).length}</span></div>
                </div>
                <div className="bg-[var(--dt-surface)] px-4 py-3 col-span-2 md:col-span-1">
                    <div className="label">As of</div>
                    <div className="mt-1 text-lg font-medium num">{m.as_of}</div>
                </div>
            </div>
            <p className="text-xs text-faint num">
                Generated {m.generated_at.replace('T', ' ')} in {m.duration_s}s ·{' '}
                {Object.entries(m.row_counts).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(' · ')}
            </p>

            <Panel title="Checks"
                actions={<Segmented<'all' | Severity> value={filter} onChange={setFilter}
                    options={[{ value: 'all', label: 'All' }, { value: 'error', label: 'Errors' }, { value: 'warning', label: 'Warnings' }, { value: 'info', label: 'Info' }]} />}>
                <div className="overflow-x-auto">
                    <table className="data-table">
                        <thead>
                            <tr><th className="w-28">Severity</th><th className="w-72">Check</th><th>What it means</th><th className="r w-24">Findings</th></tr>
                        </thead>
                        <tbody>
                            {checks.map(([name, c]) => (
                                <Fragment key={name}>
                                    <tr onClick={() => c.count && setOpen(open === name ? null : name)}
                                        className={c.count ? 'cursor-pointer' : 'text-faint'}>
                                        <td><SeverityDot severity={c.severity} /></td>
                                        <td className="text-[13px]">
                                            <span className="text-faint mr-1.5 inline-block w-2">{c.count ? (open === name ? '−' : '+') : ''}</span>
                                            {name}
                                        </td>
                                        <td className={c.count ? 'text-muted-foreground' : ''}>{c.description}</td>
                                        <td className="r num">{c.count.toLocaleString()}</td>
                                    </tr>
                                    {open === name && (
                                        <tr>
                                            <td colSpan={4} className="!bg-[var(--dt-bg)] !py-3">
                                                <ul className="space-y-1 pl-28">
                                                    {(summary.samples[name] ?? []).map((s, i) => (
                                                        <li key={i} className="flex gap-4 text-xs">
                                                            <span className="w-14 shrink-0 font-medium">
                                                                {s.ticker ? <Link className="hover:text-[var(--dt-accent)]" href={`/dashboard/ticker/${s.ticker}`} prefetch={tickerPrefetch(s.ticker)}>{s.ticker}</Link> : '—'}
                                                            </span>
                                                            <span className="text-muted-foreground num">{s.message}</span>
                                                        </li>
                                                    ))}
                                                    {c.count > (summary.samples[name]?.length ?? 0) && (
                                                        <li className="text-xs text-faint">… and {(c.count - (summary.samples[name]?.length ?? 0)).toLocaleString()} more in findings.csv</li>
                                                    )}
                                                </ul>
                                            </td>
                                        </tr>
                                    )}
                                </Fragment>
                            ))}
                        </tbody>
                    </table>
                </div>
            </Panel>
        </div>
    )
}
