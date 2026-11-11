"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const money = (x: number | null | undefined) => (x ? x.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "–");

export default function Customers() {
  const [rows, setRows] = useState<any[]>([]);
  const [accounts, setAccounts] = useState<any[]>([]);
  const [form, setForm] = useState({ account_id: "", name: "", amount: "", end_date: "", products: "" });
  const [msg, setMsg] = useState("");

  const load = () => {
    api<any[]>("/api/customers").then(setRows).catch((e) => setMsg(e.message));
    api<any[]>("/api/accounts").then(setAccounts).catch(() => {});
  };
  useEffect(load, []);

  async function act(fn: () => Promise<any>, ok: (r: any) => string) {
    setMsg("");
    try { const r = await fn(); setMsg(ok(r)); load(); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h1>Customers</h1>
      <p className="sub">Contracts, renewals and customer health. Renewal deals open automatically before a contract ends;
        customers whose health drops get a churn-risk signal. Won deals with recurring products become contracts automatically.</p>
      {msg && <p className="muted">{msg}</p>}
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={() => act(() => post("/api/customers/renewals/run"),
          (r) => `${r.renewal_deals_created} renewal deals opened, ${r.churn_risk_signals} churn-risk signals.`)}>Check renewals and health now</button>
      </div>
      <table>
        <thead><tr><th>Customer</th><th>Health</th><th>Why</th><th>Annual value</th><th>Next renewal</th><th>Upsell ideas</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.account_id}>
              <td>{r.account}</td>
              <td><span className={`pill ${r.health.churn_risk === "high" ? "tier-D" : r.health.churn_risk === "medium" ? "tier-C" : "tier-A"}`}>
                {r.health.score}</span> {r.health.churn_risk} risk</td>
              <td className="muted">{r.health.reasons.join("; ")}</td>
              <td>{money(r.arr)}</td>
              <td>{r.next_renewal ?? "–"}</td>
              <td className="muted">{r.upsell_ideas.slice(0, 3).map((u: any) => u.product).join(", ") || "–"}</td>
            </tr>
          ))}
          {!rows.length && <tr><td colSpan={6} className="muted">No customers with active contracts yet.</td></tr>}
        </tbody>
      </table>

      <h2>Add a contract</h2>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <select style={{ maxWidth: 240 }} value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })}>
          <option value="">Customer account</option>
          {accounts.map((a) => <option key={a.id ?? a.account_id} value={a.id ?? a.account_id}>{a.name}</option>)}
        </select>
        <input style={{ maxWidth: 220 }} placeholder="Contract name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input style={{ maxWidth: 140 }} placeholder="Annual value" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
        <input style={{ maxWidth: 170 }} type="date" value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} />
        <input style={{ maxWidth: 260 }} placeholder="Products (comma-separated)" value={form.products} onChange={(e) => setForm({ ...form, products: e.target.value })} />
        <button className="primary" disabled={!form.account_id || !form.name || !form.end_date} onClick={() => act(() => post("/api/contracts", {
          account_id: Number(form.account_id), name: form.name, amount: form.amount ? Number(form.amount) : null, end_date: form.end_date,
          products: form.products.split(",").map((s) => s.trim()).filter(Boolean),
        }), () => "Contract added.")}>Add contract</button>
      </div>
    </>
  );
}
