import TerminalLayout from "@/components/TerminalLayout"
import { SearchProvider } from "@/contexts/SearchContext"
import { PortfolioProvider } from "@/contexts/PortfolioContext"

export default function Layout({ children }: { children: React.ReactNode }) {
    return (
        <SearchProvider>
            <PortfolioProvider>
                <TerminalLayout>{children}</TerminalLayout>
            </PortfolioProvider>
        </SearchProvider>
    )
}
