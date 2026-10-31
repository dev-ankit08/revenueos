"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const money = (x: number | null | undefined) => (x == null ? "–" : `$${x.toFixed(2)}`);

export default function Governance() {
  const [data, setData] = useState<any>(null);
  const [edits, setEdits] = useState<Record<string, any>>({});
  const [msg, setMsg] = useState("");

  const load = () => api("/api/governance").then((d) => { setData(d); setEdits({}); }).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  const row = (agent: string, current: any) => ({ ...current, ...(edits[agent] ?? {}) });
  const change = (agent: string, patch: object) => setEdits((e) => ({ ...e, [agent]: { ...(e[agent] ?? {}), ...patch } }));

  async function save(agent: string, current: any) {
    const r = row(agent, current);
    try {
      await api(`/api/governance/${encodeURIComponent(agent)}`, {
        method: "PUT",
        body: JSON.stringify({
          model: r.model || null,
          monthly_budget_usd: r.budget === "" || r.budget == null ? null : Number(r.budget),
          enabled: r.enabled ?? true,
        }),
      });
      setMsg("Saved.");
      load();
    } catch (e: any) { setMsg(e.message); }
  }

  if (!data) return <p className="muted">{msg || "Loading…"}</p>;
  const total = row("*", { budget: data.total.budget, model: null, enabled: true });
  const pct = data.total.budget ? Math.min(100, (data.total.spent / data.total.budget) * 100) : null;

  return (
    <>
      <h1>AI governance</h1>
      <p className="sub">Monthly budgets, which model each assistant uses, and on/off switches. Agents stop when a budget is reached; a warning goes to Slack at 80%.</p>
      {msg && <p className="muted">{msg}</p>}

      <div className="card row" style={{ justifyContent: "space-between" }}>
        <div>
          <b>Total AI spend, {data.month}:</b> {money(data.total.spent)}
          {data.total.budget != null && <> of {money(data.total.budget)} ({pct?.toFixed(0)}%)</>}
        </div>
        <div className="row">
          <input style={{ width: 140 }} inputMode="decimal" placeholder="No total budget"
            value={total.budget ?? ""} onChange={(e) => change("*", { budget: e.target.value })} />
          <button className="primary" onClick={() => save("*", { budget: data.total.budget })}>Save total budget</button>
        </div>
      </div>

      <h2>Per assistant</h2>
      <table>
        <thead><tr><th>Assistant</th><th>Spent this month</th><th>Monthly budget (USD)</th><th>Model</th><th>On</th><th></th></tr></thead>
        <tbody>
          {data.agents.map((a: any) => {
            const r = row(a.agent, a);
            return (
              <tr key={a.agent}>
                <td>{a.label}{a.uses_web && <div className="muted">searches the web</div>}</td>
                <td>{money(a.spent)}{a.budget != null && <div className="muted">of {money(a.budget)}</div>}</td>
                <td><input style={{ width: 110 }} inputMode="decimal" placeholder="none" value={r.budget ?? ""}
                  onChange={(e) => change(a.agent, { budget: e.target.value })} /></td>
                <td>
                  <select style={{ width: 170 }} value={r.model ?? ""} onChange={(e) => change(a.agent, { model: e.target.value })}>
                    <option value="">default</option>
                    {data.models.map((m: string) => (
                      <option key={m} value={m} disabled={a.uses_web && m.includes("haiku")}>{m}</option>
                    ))}
                  </select>
                </td>
                <td><input type="checkbox" style={{ width: "auto" }} checked={r.enabled}
                  onChange={(e) => change(a.agent, { enabled: e.target.checked })} /></td>
                <td><button disabled={!edits[a.agent]} onClick={() => save(a.agent, a)}>Save</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
