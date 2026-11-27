import type { NextConfig } from "next";
import fs from "node:fs";
import path from "node:path";

// Backend origin the Next server proxies to. Override with BACKEND_ORIGIN
// (e.g. http://backend:8000 inside Docker). Defaults to the local backend.
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Proxy /api/* to the FastAPI backend so the browser only ever talks to the
  // frontend origin. This makes a single public tunnel (Cloudflare) sufficient
  // for remote testers — no separate backend URL or CORS juggling required.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_ORIGIN}/:path*`,
      },
    ];
  },
};

// ---------------------------------------------------------------------------
// Static demo build: NEXT_DEMO=1 next build  (or scripts/build_demo.sh)
// Exports a backend-free copy for GitHub Pages under /demo/dilution. /api/*
// GETs are answered from the snapshot recorded by scripts/record_demo.py
// (see src/lib/demo.ts). Without NEXT_DEMO the config above is used unchanged.
// ---------------------------------------------------------------------------
function demoConfig(): NextConfig {
  const basePath = process.env.DEMO_BASE_PATH ?? "/demo/dilution";
  const indexFile = path.join(process.cwd(), "demo-snapshot", "data", "_index.json");
  let manifest: { asOf?: string; tickers?: { ticker: string; title: string }[] } = {};
  try {
    manifest = JSON.parse(fs.readFileSync(indexFile, "utf8"));
  } catch {
    console.warn(`[demo] no snapshot at ${indexFile}; run scripts/record_demo.py first`);
  }
  return {
    output: "export",
    basePath,
    assetPrefix: basePath,
    trailingSlash: true,
    images: { unoptimized: true },
    // Picks up demo-only route files such as ticker/[symbol]/layout.demo.tsx.
    pageExtensions: ["demo.tsx", "tsx", "ts", "jsx", "js"],
    env: {
      DEMO_MODE: "1",
      DEMO_BASE_PATH: basePath,
      DEMO_AS_OF: manifest.asOf ?? "",
      DEMO_TICKERS: JSON.stringify(manifest.tickers ?? []),
    },
  };
}

export default process.env.NEXT_DEMO === "1" ? demoConfig() : nextConfig;
