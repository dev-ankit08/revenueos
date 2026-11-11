"use client";
import { useEffect, useState } from "react";
import { api, API_URL, post, TOKEN_KEY } from "@/lib/api";

export default function Login() {
  const [setup, setSetup] = useState(false);
  const [sso, setSso] = useState(false);
  const [needCode, setNeedCode] = useState(false);
  const [form, setForm] = useState({ email: "", password: "", name: "", code: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Coming back from single sign-on: the token arrives in the URL fragment (never sent to a server).
    const m = window.location.hash.match(/sso_token=([^&]+)/);
    if (m) {
      try { localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1])); } catch {}
      window.location.replace("/");
      return;
    }
    api("/api/auth/me").then((r) => { setSetup(r.setup_needed); setSso(!!r.sso_available); }).catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = { ...form, code: form.code || undefined };
      const r = await post(setup ? "/api/auth/bootstrap" : "/api/auth/login", body);
      try { localStorage.setItem(TOKEN_KEY, r.token); } catch {}
      window.location.href = setup ? "/onboarding" : "/";
    } catch (err: any) {
      if (err.detail?.mfa_required) setNeedCode(true);
      setError(err.message);
    }
    setBusy(false);
  }

  return (
    <div style={{ maxWidth: 380, margin: "10vh auto", padding: "0 16px" }}>
      <h1>RevenueOS</h1>
      <p className="sub">{setup ? "Create the owner account to get started." : "Sign in to your workspace."}</p>
      <form className="card stack" onSubmit={submit}>
        {setup && <input placeholder="Your name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        <input type="email" required placeholder="Work email" autoComplete="username" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input type="password" required minLength={setup ? 10 : 1} placeholder={setup ? "Password (10+ characters)" : "Password"}
          autoComplete={setup ? "new-password" : "current-password"} value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
        {needCode && <input inputMode="numeric" autoComplete="one-time-code" placeholder="6-digit code from your authenticator app"
          value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} autoFocus />}
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>{setup ? "Create account" : "Sign in"}</button>
        {!setup && sso && <a className="button" href={`${API_URL}/api/auth/oidc/start`} style={{ textAlign: "center" }}>Sign in with your company account</a>}
      </form>
    </div>
  );
}
