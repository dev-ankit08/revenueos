"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const pct = (x: number | null | undefined, digits = 1) => (x == null ? "–" : `${(x * 100).toFixed(digits)}%`);

export default function Deliverability() {
  const [data, setData] = useState<any>(null);
  const [connections, setConnections] = useState<any[]>([]);
  const [form, setForm] = useState({ address: "", provider: "smtp", owner: "", daily_cap: "", warmup_days: "21" });
  const [domain, setDomain] = useState("");
  const [dns, setDns] = useState<any>(null);
  const [test, setTest] = useState<any>(null);
  const [msg, setMsg] = useState("");

  const load = () => {
    api("/api/deliverability").then(setData).catch((e) => setMsg(e.message));
    api<any[]>("/api/integrations/connections").then(setConnections).catch(() => {});
  };
  useEffect(load, []);

  async function act(fn: () => Promise<any>, ok: string) {
    setMsg("");
    try { await fn(); setMsg(ok); load(); } catch (e: any) { setMsg(e.message); }
  }

  async function checkDns() {
    setMsg(""); setDns(null);
    try { setDns(await api(`/api/deliverability/dns?domain=${encodeURIComponent(domain)}`)); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h1>Deliverability</h1>
      <p className="sub">Keeps your emails out of spam and protects your domains. Sends are spread across a pool of mailboxes,
        each with a daily limit and a warm-up period; a mailbox pauses itself if spam complaints or bounces rise.</p>
      {msg && <p className="muted">{msg}</p>}

      {data && (
        <div className="grid">
          <div className="card stat"><div className="label">Can send today</div><div className="value">{data.capacity_today}</div></div>
          <div className="card stat"><div className="label">Sent in last 24h</div><div className="value">{data.sent_24h}</div></div>
          <div className="card stat"><div className="label">Paused mailboxes</div><div className="value">{data.paused}</div></div>
          <div className="card stat"><div className="label">Inbox placement</div>
            <div className="value">{data.latest_placement?.placement_pct != null ? `${data.latest_placement.placement_pct}%` : "–"}</div></div>
        </div>
      )}

      <h2>Sending mailboxes</h2>
      {data && !data.mailboxes.length && (
        <p className="muted">No pool yet: every email goes from the single sender address in Settings. Add 2+ mailboxes on a
          separate outreach domain to spread the load (about 40 cold emails per mailbox per day is a safe limit).</p>
      )}
      {data?.mailboxes.length > 0 && (
        <table>
          <thead><tr><th>Mailbox</th><th>Status</th><th>Today</th><th>Sent 30d</th><th>Bounces</th><th>Complaints</th><th></th></tr></thead>
          <tbody>{data.mailboxes.map((m: any) => (
            <tr key={m.id}>
              <td>{m.address}<div className="muted">{m.provider}{m.owner ? ` · ${m.owner}` : ""}</div>
                {m.cold_domain_warning && <div className="flag warn">{m.cold_domain_warning}</div>}</td>
              <td><span className={`pill ${m.status === "active" ? "tier-A" : m.status === "warming" ? "tier-B" : "tier-D"}`}>{m.status}</span>
                {m.paused_reason && <div className="muted">{m.paused_reason}</div>}</td>
              <td>{m.health.sent_24h} / {m.health.today_cap}{m.status === "warming" && <div className="muted">warming up</div>}</td>
              <td>{m.health.sent_30d}</td>
              <td>{pct(m.health.bounce_rate)}</td>
              <td className={(m.health.complaint_rate ?? 0) > 0.001 ? "error" : ""}>{pct(m.health.complaint_rate, 2)}</td>
              <td className="row">
                {m.status === "paused"
                  ? <button onClick={() => act(() => post(`/api/deliverability/mailboxes/${m.id}/resume`), "Resumed.")}>Resume</button>
                  : <button onClick={() => act(() => post(`/api/deliverability/mailboxes/${m.id}/pause`), "Paused.")}>Pause</button>}
                <button className="danger" onClick={() => confirm(`Remove ${m.address} from the pool?`) &&
                  act(() => post(`/api/deliverability/mailboxes/${m.id}/remove`), "Removed.")}>Remove</button>
              </td>
            </tr>
          ))}</tbody>
        </table>
      )}
      <div className="card row" style={{ flexWrap: "wrap", marginTop: 12 }}>
        <input style={{ maxWidth: 260 }} placeholder="Mailbox address" value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} />
        <select style={{ maxWidth: 230 }} value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
          <option value="smtp">Through our sending service</option>
          <option value="google">Rep's own Gmail (connected)</option>
          <option value="microsoft">Rep's own Outlook (connected)</option>
        </select>
        <input style={{ maxWidth: 200 }} placeholder="Owner (rep email)" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} />
        <input style={{ maxWidth: 120 }} placeholder="Daily limit (40)" value={form.daily_cap} onChange={(e) => setForm({ ...form, daily_cap: e.target.value })} />
        <select style={{ maxWidth: 190 }} value={form.warmup_days} onChange={(e) => setForm({ ...form, warmup_days: e.target.value })}>
          <option value="21">New: warm up over 21 days</option>
          <option value="14">Warm up over 14 days</option>
          <option value="0">Already warm</option>
        </select>
        <button className="primary" disabled={!form.address} onClick={() => act(() => post("/api/deliverability/mailboxes", {
          address: form.address, provider: form.provider, owner: form.owner || null,
          daily_cap: form.daily_cap ? Number(form.daily_cap) : null, warmup_days: Number(form.warmup_days),
        }), "Mailbox added.")}>Add mailbox</button>
      </div>
      {(form.provider === "google" || form.provider === "microsoft") &&
        <p className="muted">The rep connects this mailbox first on Integrations ({connections.filter((c) => c.provider === form.provider).length} connected).
          Reconnect older connections once so RevenueOS gets permission to send approved emails.</p>}

      <h2>Domain check</h2>
      <p className="muted">Gmail, Yahoo and Microsoft require SPF, DKIM and DMARC on domains that send in volume.</p>
      <div className="row">
        <input style={{ maxWidth: 280 }} placeholder="Sending domain, e.g. getcriticalriver.com" value={domain} onChange={(e) => setDomain(e.target.value)} />
        <button disabled={!domain} onClick={checkDns}>Check domain</button>
      </div>
      {dns && (
        <div className="card stack" style={{ marginTop: 12 }}>
          <div><b>{dns.domain}</b>: <span className={`pill ${dns.score >= 85 ? "tier-A" : dns.score >= 50 ? "tier-C" : "tier-D"}`}>{dns.score}/100</span> {dns.verdict}</div>
          <div className="row">
            {["spf", "dkim", "dmarc", "mx"].map((k) => <span key={k} className={`pill ${dns.checks[k].ok ? "tier-A" : "tier-D"}`}>{k.toUpperCase()} {dns.checks[k].ok ? "ok" : "missing"}</span>)}
          </div>
          {dns.fixes.map((f: string, i: number) => <div key={i} className="flag warn">{f}</div>)}
        </div>
      )}

      <h2>Inbox placement test</h2>
      <p className="muted">Sends a short test email from every mailbox in the pool to your team's connected Gmail/Outlook
        mailboxes, then checks whether each landed in the inbox, promotions or spam. Healthy is 80%+ in the inbox.</p>
      <div className="row">
        <button onClick={() => act(async () => setTest(await post("/api/deliverability/placement-tests")), "Test emails sent. Check results in 3-5 minutes.")}>Run a test</button>
        {test && <button onClick={() => act(async () => setTest(await api(`/api/deliverability/placement-tests/${test.id}`)), "Results updated.")}>Check results</button>}
      </div>
      {test && (
        <table style={{ marginTop: 8 }}>
          <thead><tr><th>From</th><th>To (test inbox)</th><th>Landed in</th></tr></thead>
          <tbody>{test.results.map((r: any, i: number) => (
            <tr key={i}><td>{r.from}</td><td>{r.seed}</td>
              <td className={r.result === "spam" ? "error" : ""}>{r.result.replace("_", " ")}</td></tr>
          ))}</tbody>
        </table>
      )}
    </>
  );
}
