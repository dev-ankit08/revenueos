"use client";
import { useEffect, useState } from "react";
import { api, post, TOKEN_KEY } from "@/lib/api";

export default function Login() {
  const [setup, setSetup] = useState(false);
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api("/api/auth/me").then((m) => setSetup(m.setup_needed)).catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await post(setup ? "/api/auth/bootstrap" : "/api/auth/login", form);
      try { localStorage.setItem(TOKEN_KEY, r.token); } catch {}
      window.location.href = setup ? "/onboarding" : "/";
    } catch (err: any) { setError(err.message); }
    setBusy(false);
  }

  return (
    <div style={{ maxWidth: 380, margin: "10vh auto" }}>
      <h1>RevenueOS</h1>
      <p className="sub">{setup ? "Create the owner account to get started." : "Sign in to your workspace."}</p>
      <form className="card stack" onSubmit={submit}>
        {setup && <input placeholder="Your name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        <input type="email" required placeholder="Work email" autoComplete="username" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input type="password" required minLength={setup ? 10 : 1} placeholder={setup ? "Password (10+ characters)" : "Password"}
          autoComplete={setup ? "new-password" : "current-password"} value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>{setup ? "Create account" : "Sign in"}</button>
      </form>
    </div>
  );
}
