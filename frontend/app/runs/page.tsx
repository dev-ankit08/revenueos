"use client";
import { Fragment, useEffect, useState } from "react";
import { api } from "@/lib/api";

const time = (d: string | null) => (d ? new Date(d).toLocaleString() : "");

function seconds(r: any): string {
  if (!r.started_at || !r.finished_at) return "";
  return `${Math.max(0, Math.round((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000))}s`;
}

export default function Runs() {
  const [runs, setRuns] = useState<any[]>([]);
  const [open, setOpen] = useState<Record<number, any>>({});
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  useEffect(() => { api<any[]>("/api/runs?limit=200").then(setRuns).catch((e) => setError(e.message)); }, []);

  async function toggle(id: number) {
    if (open[id]) return setOpen((o) => { const { [id]: _, ...rest } = o; return rest; });
    const r = await api(`/api/runs/${id}`);
    setOpen((o) => ({ ...o, [id]: r }));
  }

  const shown = runs.filter((r) => !filter || (filter === "failed" ? r.status === "failed" : r.agent.includes(filter)));
  return (
    <>
      <h1>Activity log</h1>
      <p className="sub">Every assistant run and background job: what it was asked, what it did, how long it took and what it cost.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <select style={{ maxWidth: 220 }} value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">all runs</option>
          <option value="failed">failed only</option>
          {[...new Set(runs.map((r) => r.agent))].sort().map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <span className="muted">{shown.length} runs</span>
      </div>
      {error && <p className="error">{error}</p>}
      <table>
        <thead><tr><th>When</th><th>Assistant / job</th><th>Status</th><th>Time</th><th>Cost</th></tr></thead>
        <tbody>
          {shown.map((r) => (
            <Fragment key={r.id}>
              <tr onClick={() => toggle(r.id)} style={{ cursor: "pointer" }}>
                <td>{time(r.started_at)}</td><td>{r.agent}</td>
                <td className={r.status === "failed" ? "error" : ""}>{r.status}</td>
                <td>{seconds(r)}</td><td>${(r.cost_usd ?? 0).toFixed(4)}</td>
              </tr>
              {open[r.id] && (
                <tr><td colSpan={5}>
                  <div className="stack">
                    <div><b>Asked:</b> <span style={{ whiteSpace: "pre-wrap" }}>{String(open[r.id].input ?? "").slice(0, 1500)}</span></div>
                    {open[r.id].output && <div><b>Result:</b> <span style={{ whiteSpace: "pre-wrap" }}>{String(open[r.id].output).slice(0, 1500)}</span></div>}
                    {open[r.id].error && <div className="error">{open[r.id].error}</div>}
                    {open[r.id].children?.length > 0 && (
                      <div><b>Steps:</b>
                        <ul>{open[r.id].children.map((c: any) => (
                          <li key={c.id}>{c.agent}: {c.status} · {seconds(c)} · ${(c.cost_usd ?? 0).toFixed(4)}{c.error ? ` · ${c.error}` : ""}</li>
                        ))}</ul>
                      </div>
                    )}
                  </div>
                </td></tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </>
  );
}
