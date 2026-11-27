"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import api from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import Link from "next/link"
import { IS_DEMO } from "@/lib/demo"

function formatApiError(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (typeof detail === "string") return detail
    if (Array.isArray(detail)) {
        return detail
            .map((item) => {
                if (item && typeof item === "object" && "msg" in item) {
                    return String((item as { msg: string }).msg)
                }
                return JSON.stringify(item)
            })
            .join(" ")
    }
    return fallback
}

export default function AuthPage() {
    const router = useRouter()
    const [error, setError] = useState("")
    const [loading, setLoading] = useState(false)

    const [loginForm, setLoginForm] = useState({ username: "", password: "" })
    const [signupForm, setSignupForm] = useState({ username: "", email: "", full_name: "", password: "" })

    const handleLogin = async (e: React.FormEvent) => {
        e.preventDefault()
        setError("")
        setLoading(true)
        try {
            const res = await api.post("/auth/login", {
                username_or_email: loginForm.username.trim(),
                password: loginForm.password
            })
            localStorage.setItem("token", res.data.access_token)
            router.push("/dashboard")
        } catch (err: unknown) {
            console.error(err);
            setError(formatApiError(err, "Sign-in failed."))
        } finally {
            setLoading(false)
        }
    }

    const handleSignup = async (e: React.FormEvent) => {
        e.preventDefault()
        setError("")
        setLoading(true)
        try {
            await api.post("/auth/signup", {
                ...signupForm,
                username: signupForm.username.trim(),
                email: signupForm.email.trim(),
            })
            const res = await api.post("/auth/login", {
                username_or_email: signupForm.username.trim(),
                password: signupForm.password
            })
            localStorage.setItem("token", res.data.access_token)
            router.push("/dashboard")
        } catch (err: unknown) {
            console.error(err);
            setError(formatApiError(err, "Could not create the account."))
        } finally {
            setLoading(false)
        }
    }

    if (IS_DEMO) {
        return (
            <div className="min-h-screen bg-background flex flex-col">
                <header className="h-12 border-b border-border px-4 md:px-6 flex items-center">
                    <Link href="/" className="text-[14px] font-semibold tracking-tight">Dilution Terminal</Link>
                </header>
                <div className="flex-1 grid place-items-center px-4 py-12">
                    <div className="w-full max-w-sm panel p-5 text-[13px] text-muted-foreground space-y-3">
                        <h1 className="text-lg font-semibold text-foreground">Accounts are off in this snapshot</h1>
                        <p>This static copy has no backend, so sign-in and sign-up are disabled. You are browsing as a read-only demo user.</p>
                        <div className="flex gap-4">
                            <Link href="/dashboard" className="text-[var(--dt-accent)] hover:underline">Demo portfolio</Link>
                            <Link href="/dashboard/rankings" className="text-[var(--dt-accent)] hover:underline">Rankings</Link>
                        </div>
                    </div>
                </div>
            </div>
        )
    }

    const errorBox = error && (
        <p role="alert" className="text-[13px] px-3 py-2 rounded-md border border-[color-mix(in_srgb,var(--dt-down)_40%,transparent)] text-[var(--dt-down)]">{error}</p>
    )
    const inputCls = "h-9 bg-[var(--dt-bg)] shadow-none focus-visible:ring-0 focus-visible:border-[var(--dt-accent)]"
    const tabCls = "text-[13px] data-[state=active]:bg-[var(--dt-hover)] data-[state=active]:shadow-none rounded-[4px]"

    return (
        <div className="min-h-screen bg-background flex flex-col">
            <header className="h-12 border-b border-border px-4 md:px-6 flex items-center justify-between">
                <Link href="/" className="flex items-center gap-2">
                    <span className="h-4 w-4 rounded-[3px] border border-[var(--dt-accent)] grid place-items-center"><span className="h-1.5 w-1.5 bg-[var(--dt-accent)]" /></span>
                    <span className="text-[14px] font-semibold tracking-tight">Dilution Terminal</span>
                </Link>
                <Link href="/dashboard/rankings" className="text-[13px] text-muted-foreground hover:text-foreground">Browse without an account</Link>
            </header>
            <div className="flex-1 grid place-items-center px-4 py-12">
                <div className="w-full max-w-sm">
                    <h1 className="text-lg font-semibold">Sign in</h1>
                    <p className="mt-1 text-[13px] text-muted-foreground">An account lets you keep a portfolio and receive dilution alerts.</p>
                    <div className="panel mt-6 p-4">
                        <Tabs defaultValue="login" className="w-full">
                            <TabsList className="grid w-full grid-cols-2 bg-[var(--dt-bg)] border border-border h-8 p-0.5">
                                <TabsTrigger value="login" className={tabCls}>Sign in</TabsTrigger>
                                <TabsTrigger value="signup" className={tabCls}>Create account</TabsTrigger>
                            </TabsList>

                            <TabsContent value="login">
                                <form onSubmit={handleLogin} className="space-y-4 mt-4">
                                    <div className="space-y-1.5">
                                        <Label htmlFor="username" className="label">Username or email</Label>
                                        <Input id="username" autoComplete="username" className={inputCls}
                                            value={loginForm.username}
                                            onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })} required />
                                    </div>
                                    <div className="space-y-1.5">
                                        <Label htmlFor="password" className="label">Password</Label>
                                        <Input id="password" type="password" autoComplete="current-password" className={inputCls}
                                            value={loginForm.password}
                                            onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })} required />
                                    </div>
                                    {errorBox}
                                    <Button type="submit" className="w-full h-9 shadow-none" disabled={loading}>
                                        {loading ? "Signing in…" : "Sign in"}
                                    </Button>
                                </form>
                            </TabsContent>

                            <TabsContent value="signup">
                                <form onSubmit={handleSignup} className="space-y-4 mt-4">
                                    <div className="space-y-1.5">
                                        <Label htmlFor="s_user" className="label">Username</Label>
                                        <Input id="s_user" autoComplete="username" className={inputCls}
                                            value={signupForm.username}
                                            onChange={(e) => setSignupForm({ ...signupForm, username: e.target.value })} required />
                                    </div>
                                    <div className="space-y-1.5">
                                        <Label htmlFor="s_email" className="label">Email</Label>
                                        <Input id="s_email" type="email" autoComplete="email" className={inputCls}
                                            value={signupForm.email}
                                            onChange={(e) => setSignupForm({ ...signupForm, email: e.target.value })} required />
                                    </div>
                                    <div className="space-y-1.5">
                                        <Label htmlFor="s_pass" className="label">Password</Label>
                                        <Input id="s_pass" type="password" autoComplete="new-password" minLength={8} className={inputCls}
                                            value={signupForm.password}
                                            onChange={(e) => setSignupForm({ ...signupForm, password: e.target.value })} required />
                                        <p className="text-xs text-faint">
                                            At least 8 characters. Username: 3–32 letters, numbers, dots, hyphens or underscores.
                                        </p>
                                    </div>
                                    {errorBox}
                                    <Button type="submit" className="w-full h-9 shadow-none" disabled={loading}>
                                        {loading ? "Creating account…" : "Create account"}
                                    </Button>
                                </form>
                            </TabsContent>
                        </Tabs>
                    </div>
                </div>
            </div>
        </div>
    )
}
