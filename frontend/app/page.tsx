"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const pct = (v: number | null) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);

export default function Dashboard() {
  const [data, setData] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/api/analytics").then(setData).catch((e) => setError(e.message));
    api("/api/health").then(setHealth).catch(() => {});
  }, []);

  if (error) return <p className="error">Cannot reach the API: {error}</p>;
  if (!data) return <p className="muted">Loading…</p>;

  const stats: [string, string | number][] = [
    ["Leads", data.leads.total],
    ["A/B-tier leads", (data.leads.by_tier.A ?? 0) + (data.leads.by_tier.B ?? 0)],
    ["Drafts awaiting review", data.outreach.pending_review],
    ["Emails sent", data.outreach.sent],
    ["Reply rate", pct(data.outreach.reply_rate)],
    ["Meetings", data.outreach.meetings],
    ["Draft approval rate", pct(data.outreach.approval_rate)],
    ["Account briefs", data.ai.briefs],
    ["Est. hours saved", data.ai.est_hours_saved],
    ["AI spend", `$${data.ai.total_cost_usd}`],
  ];

  return (
    <>
      <h1>Revenue dashboard</h1>
      <p className="sub">
        {health && <>CRM: <b>{health.crm}</b> · Email: <b>{health.email}</b> · </>}
        Agents research, score and draft; humans approve every send.
      </p>
      <div className="grid">
        {stats.map(([label, value]) => (
          <div className="card stat" key={label}>
            <div className="label">{label}</div>
            <div className="value">{value}</div>
          </div>
        ))}
      </div>

      <h2>Pipeline by stage</h2>
      <div className="grid">
        {["new", "researched", "sequenced", "contacted", "replied", "meeting", "disqualified"].map((s) => (
          <div className="card stat" key={s}>
            <div className="label">{s}</div>
            <div className="value">{data.leads.by_status[s] ?? 0}</div>
          </div>
        ))}
      </div>

      <h2>AI cost by agent</h2>
      <table>
        <thead><tr><th>Agent</th><th>Runs</th><th>Cost (USD)</th></tr></thead>
        <tbody>
          {data.ai.by_agent.map((r: any) => (
            <tr key={r.agent}><td>{r.agent}</td><td>{r.runs}</td><td>${r.cost_usd.toFixed(4)}</td></tr>
          ))}
          {!data.ai.by_agent.length && <tr><td colSpan={3} className="muted">No agent runs yet. Try the Copilot.</td></tr>}
        </tbody>
      </table>
    </>
  );
}
