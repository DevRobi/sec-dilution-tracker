import axios from 'axios';
import { IS_DEMO, installDemoFetchShim } from './demo';

// Demo build only: answer /api/* from the static snapshot (see lib/demo.ts).
installDemoFetchShim();

function normalizeBase(url: string): string {
    return url.trim().replace(/\/+$/, '');
}

export function getApiBaseCandidates(): string[] {
    const envBase = process.env.NEXT_PUBLIC_API_BASE_URL;
    const bases: string[] = [];

    if (envBase) {
        bases.push(normalizeBase(envBase));
    }

    // Default: same-origin '/api', proxied to the backend by Next.js rewrites
    // (see next.config.ts). This works locally AND behind a public tunnel
    // without hardcoding host/port, so remote testers need no extra config.
    bases.push('/api');

    return Array.from(new Set(bases));
}

export function getPrimaryApiBase(): string {
    return getApiBaseCandidates()[0];
}

export const DATA_VERSION = 'split-adj-1';

const api = axios.create({
    baseURL: getPrimaryApiBase(),
    // The demo snapshot is served by a window.fetch shim, so route axios through fetch there.
    ...(IS_DEMO ? { adapter: 'fetch' as const } : {}),
    headers: {
        'Content-Type': 'application/json',
    },
});

// Request interceptor to add token
api.interceptors.request.use(
    (config) => {
        if (typeof window !== 'undefined') {
            config.baseURL = getPrimaryApiBase();
        }

        // Bump DATA_VERSION when the meaning of API data changes (e.g. share counts
        // became split-adjusted) so browsers don't serve stale cached responses.
        if ((config.method ?? 'get').toLowerCase() === 'get') {
            config.params = { ...(config.params ?? {}), v: DATA_VERSION };
        }

        const token = typeof window !== 'undefined' ? window.localStorage.getItem('token') : null;
        if (token) {
            config.headers = config.headers ?? {};
            config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
    },
    (error) => {
        return Promise.reject(error);
    }
);

export default api;
