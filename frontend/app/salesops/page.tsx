"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function SalesSetup() {
  const [territories, setTerritories] = useState<any[]>([]);
  const [quotas, setQuotas] = useState<any[]>([]);
  const [products, setProducts] = useState<any[]>([]);
  const [msg, setMsg] = useState("");
  const [t, setT] = useState({ name: "", priority: "100", countries: "", industries: "", min: "", max: "", members: "" });
  const [q, setQ] = useState({ owner: "", period: "", amount: "" });
  const [p, setP] = useState({ name: "", sku: "", unit_price: "", billing: "one_time", description: "" });

  const load = () => {
    api<any[]>("/api/territories").then(setTerritories).catch((e) => setMsg(e.message));
    api<any[]>("/api/quotas").then(setQuotas).catch(() => {});
    api<any[]>("/api/products").then(setProducts).catch(() => {});
  };
  useEffect(load, []);

  async function act(fn: () => Promise<any>, ok: string) {
    setMsg("");
    try { await fn(); setMsg(ok); load(); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <>
      <h1>Sales setup</h1>
      <p className="sub">Who owns which leads, each rep's quota, and the products and prices proposals may quote. Admins only.</p>
      {msg && <p className="muted">{msg}</p>}

      <h2>Territories and lead routing</h2>
      <p className="muted">New leads go to the first matching territory (lowest priority number first), to its members in turn.
        A territory with no rules catches everyone else. A lead at an account that already has an owner goes to that owner.</p>
      <table>
        <thead><tr><th>Priority</th><th>Name</th><th>Countries</th><th>Industries</th><th>Size</th><th>Members</th><th></th></tr></thead>
        <tbody>{territories.map((x) => (
          <tr key={x.id}><td>{x.priority}</td><td>{x.name}</td><td>{x.countries?.join(", ") || "any"}</td>
            <td>{x.industries?.join(", ") || "any"}</td>
            <td>{x.min_employees ?? 0}–{x.max_employees ?? "∞"}</td><td>{x.members.join(", ")}</td>
            <td><button className="danger" onClick={() => act(() => api(`/api/territories/${x.id}`, { method: "DELETE" }), "Deleted.")}>Delete</button></td></tr>
        ))}</tbody>
      </table>
      <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <input style={{ maxWidth: 180 }} placeholder="Name" value={t.name} onChange={(e) => setT({ ...t, name: e.target.value })} />
        <input style={{ maxWidth: 90 }} placeholder="Priority" value={t.priority} onChange={(e) => setT({ ...t, priority: e.target.value })} />
        <input style={{ maxWidth: 180 }} placeholder="Countries (comma)" value={t.countries} onChange={(e) => setT({ ...t, countries: e.target.value })} />
        <input style={{ maxWidth: 180 }} placeholder="Industries (comma)" value={t.industries} onChange={(e) => setT({ ...t, industries: e.target.value })} />
        <input style={{ maxWidth: 110 }} placeholder="Min staff" value={t.min} onChange={(e) => setT({ ...t, min: e.target.value })} />
        <input style={{ maxWidth: 110 }} placeholder="Max staff" value={t.max} onChange={(e) => setT({ ...t, max: e.target.value })} />
        <input style={{ maxWidth: 260 }} placeholder="Members: rep emails (comma)" value={t.members} onChange={(e) => setT({ ...t, members: e.target.value })} />
        <button className="primary" disabled={!t.name || !t.members} onClick={() => act(() => post("/api/territories", {
          name: t.name, priority: Number(t.priority) || 100, countries: list(t.countries) || null, industries: list(t.industries) || null,
          min_employees: t.min ? Number(t.min) : null, max_employees: t.max ? Number(t.max) : null, members: list(t.members),
        }), "Territory added.")}>Add territory</button>
        <button onClick={() => act(() => post("/api/routing/run"), "Unowned leads routed.")}>Route unowned leads now</button>
      </div>

      <h2>Quotas</h2>
      <table><thead><tr><th>Rep</th><th>Quarter</th><th>Quota</th></tr></thead>
        <tbody>{quotas.map((x) => <tr key={x.id}><td>{x.owner}</td><td>{x.period}</td><td>{x.amount.toLocaleString()}</td></tr>)}</tbody></table>
      <div className="row" style={{ marginTop: 8 }}>
        <input style={{ maxWidth: 240 }} placeholder="Rep email" value={q.owner} onChange={(e) => setQ({ ...q, owner: e.target.value })} />
        <input style={{ maxWidth: 120 }} placeholder="2026-Q4" value={q.period} onChange={(e) => setQ({ ...q, period: e.target.value })} />
        <input style={{ maxWidth: 140 }} placeholder="Amount" value={q.amount} onChange={(e) => setQ({ ...q, amount: e.target.value })} />
        <button className="primary" onClick={() => act(() => api("/api/quotas", { method: "PUT",
          body: JSON.stringify({ owner: q.owner, period: q.period, amount: Number(q.amount) }) }), "Quota saved.")}>Save quota</button>
      </div>

      <h2>Products and price list</h2>
      <p className="muted">Proposals quote only these products and prices. Add them to a deal as line items and the deal amount follows.</p>
      <table><thead><tr><th>Product</th><th>SKU</th><th>Price</th><th>Billing</th></tr></thead>
        <tbody>{products.map((x) => <tr key={x.id}><td>{x.name}<div className="muted">{x.description}</div></td><td>{x.sku}</td>
          <td>{x.unit_price.toLocaleString()} {x.currency}</td><td>{x.billing.replace("_", " ")}</td></tr>)}</tbody></table>
      <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <input style={{ maxWidth: 220 }} placeholder="Product name" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} />
        <input style={{ maxWidth: 120 }} placeholder="SKU" value={p.sku} onChange={(e) => setP({ ...p, sku: e.target.value })} />
        <input style={{ maxWidth: 120 }} placeholder="Price" value={p.unit_price} onChange={(e) => setP({ ...p, unit_price: e.target.value })} />
        <select style={{ maxWidth: 140 }} value={p.billing} onChange={(e) => setP({ ...p, billing: e.target.value })}>
          <option value="one_time">one time</option><option value="monthly">monthly</option><option value="annual">annual</option>
        </select>
        <input style={{ maxWidth: 300 }} placeholder="Description" value={p.description} onChange={(e) => setP({ ...p, description: e.target.value })} />
        <button className="primary" disabled={!p.name || !p.unit_price} onClick={() => act(() => post("/api/products", {
          name: p.name, sku: p.sku || null, unit_price: Number(p.unit_price), billing: p.billing, description: p.description || null,
        }), "Product added.")}>Add product</button>
      </div>
    </>
  );
}
