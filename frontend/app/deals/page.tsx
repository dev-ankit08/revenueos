"use client";
import Link from "next/link";
import { Fragment, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api, post, waitForRun } from "@/lib/api";

const STAGES = ["discovery", "qualification", "solution", "proposal", "negotiation", "closed_won", "closed_lost"];
const MEDDICC: [string, string][] = [
  ["metrics", "Metrics"], ["economic_buyer", "Economic buyer"], ["decision_criteria", "Decision criteria"],
  ["decision_process", "Decision process"], ["identify_pain", "Pain"], ["champion", "Champion"], ["competition", "Competition"],
];

export default function Deals() {
  const [deals, setDeals] = useState<any[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [strategy, setStrategy] = useState<Record<number, any>>({});
  const [reviewing, setReviewing] = useState<number | null>(null);
  const load = () => api("/api/deals").then(setDeals).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const [reasons, setReasons] = useState<{ won: string[]; lost: string[] }>({ won: [], lost: [] });
  useEffect(() => { api("/api/deals/close-reasons").then(setReasons).catch(() => {}); }, []);

  async function patch(id: number, body: any) {
    setError("");
    if (body.stage === "closed_won" || body.stage === "closed_lost") {
      const options = body.stage === "closed_won" ? reasons.won : reasons.lost;
      const picked = prompt(`Why was it ${body.stage === "closed_won" ? "won" : "lost"}? Type one of:\n${options.join(", ")}`);
      if (!picked) return;
      body.close_reason = picked.trim();
      if (body.stage === "closed_lost") {
        const who = prompt("Lost to which competitor? (optional)");
        if (who) body.competitors = [who.trim()];
      }
    }
    try {
      await api(`/api/deals/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      load();
    } catch (e: any) { setError(e.message); }
  }

  async function toggle(id: number) {
    if (open === id) return setOpen(null);
    setOpen(id);
    api(`/api/deals/${id}/strategy`).then((s) => setStrategy((m) => ({ ...m, [id]: s }))).catch(() => {});
  }

  async function review(id: number) {
    setReviewing(id);
    setError("");
    try {
      const { run_id } = await post(`/api/deals/${id}/review`);
      const r = await waitForRun(run_id);
      if (r.status === "failed") setError(`Review failed: ${r.error}`);
      const s = await api(`/api/deals/${id}/strategy`);
      setStrategy((m) => ({ ...m, [id]: s }));
      setOpen(id);
    } catch (e: any) { setError(e.message); }
    setReviewing(null);
    load();
  }

  const open_ = deals.filter((d) => !d.stage.startsWith("closed"));
  const total = open_.reduce((s, d) => s + (d.amount || 0), 0);

  return (
    <>
      <h1>Deals</h1>
      <p className="sub">
        Created and updated by the Call Analyst. {open_.length} open · pipeline {total ? total.toLocaleString() : "–"}
      </p>
      {error && <p className="error">{error}</p>}
      <table>
        <thead><tr><th>Deal</th><th>Stage</th><th>Amount</th><th>Close</th><th>Health</th><th>Win %</th><th>Next step</th><th></th></tr></thead>
        <tbody>
          {deals.map((d) => (
            <Fragment key={d.id}>
              <tr>
                <td>
                  <a href="#" onClick={(e) => { e.preventDefault(); toggle(d.id); }}><b>{d.name}</b></a>
                  <div className="muted">{d.account}{(d.sf_id || d.crm_id) && " · in CRM"}</div>
                </td>
                <td>
                  <select value={d.stage} onChange={(e) => patch(d.id, { stage: e.target.value })}>
                    {STAGES.map((s) => <option key={s}>{s}</option>)}
                  </select>
                </td>
                <td>{d.amount ? d.amount.toLocaleString() : "–"}</td>
                <td>{d.close_date ?? "–"}</td>
                <td>{d.health && <span className={`pill health-${d.health}`}>{d.health}</span>}</td>
                <td>{d.win_probability ?? "–"}{d.forecast_category && <div className="muted">{d.forecast_category}</div>}</td>
                <td>{d.next_steps?.[0] ?? <span className="muted">none</span>}</td>
                <td><button disabled={reviewing !== null} onClick={() => review(d.id)}>
                  {reviewing === d.id ? "Reviewing…" : "Review"}</button></td>
              </tr>
              {open === d.id && (
                <tr>
                  <td colSpan={8}>
                    <div className="grid">
                      {MEDDICC.map(([k, label]) => (
                        <div key={k} className={d.qualification?.[k]?.toLowerCase?.().startsWith("unknown") ? "muted" : ""}>
                          <b>{label}:</b> {d.qualification?.[k] ?? "Unknown"}
                        </div>
                      ))}
                    </div>
                    {d.risks?.length > 0 && <><h2>Risks</h2><ul>{d.risks.map((r: string) => <li key={r}>{r}</li>)}</ul></>}
                    {d.next_steps?.length > 0 && <><h2>Next steps</h2><ul>{d.next_steps.map((r: string) => <li key={r}>{r}</li>)}</ul></>}
                    {strategy[d.id]?.content && (
                      <div className="card md" style={{ marginTop: 12 }}>
                        <div className="muted">Deal Strategist · {new Date(strategy[d.id].created_at).toLocaleString()}</div>
                        <ReactMarkdown>{strategy[d.id].content}</ReactMarkdown>
                      </div>
                    )}
                    {d.primary_contact_id && <Link href={`/pipeline/${d.primary_contact_id}`}>Open primary contact →</Link>}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
          {!deals.length && <tr><td colSpan={8} className="muted">No deals yet. Analyse a call from a lead's page to create one.</td></tr>}
        </tbody>
      </table>
    </>
  );
}
