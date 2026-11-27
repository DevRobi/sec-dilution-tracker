"use client"

export type TimePeriod = '1w' | '1m' | '2m' | '6m' | '1y' | '2y' | '5y'

interface TimePeriodFilterProps {
    selected: TimePeriod
    onChange: (period: TimePeriod) => void
    periods?: TimePeriod[]
}

const defaultPeriods: TimePeriod[] = ['1w', '1m', '2m', '6m', '1y', '2y', '5y']

export function TimePeriodFilter({ selected, onChange, periods = defaultPeriods }: TimePeriodFilterProps) {
    return (
        <div className="flex gap-2 mb-4">
            {periods.map((period) => (
                <button
                    key={period}
                    onClick={() => onChange(period)}
                    className={`px-3 py-1 text-sm rounded-md transition-colors dt-num ${selected === period
                        ? 'bg-primary text-primary-foreground font-medium'
                        : 'bg-muted text-muted-foreground hover:bg-[var(--dt-border-strong)] hover:text-foreground'
                        }`}
                >
                    {period.toUpperCase()}
                </button>
            ))}
        </div>
    )
}
