import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { DemoBanner } from "@/components/DemoBanner";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Dilution Terminal",
  description: "Share-count history, buybacks and dilution for U.S. equities, built from SEC filings.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`dark ${inter.variable}`} suppressHydrationWarning>
      <body className="antialiased">
        <DemoBanner />
        {children}
      </body>
    </html>
  );
}
