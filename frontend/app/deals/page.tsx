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
                <td>{d.close_date ?? "–"}
                  {d.close_date_pushes > 0 && <div className="muted">moved {d.close_date_pushes}x</div>}
                  {(d.slip_risk === "high" || d.slip_risk === "medium") &&
                    <div className={d.slip_risk === "high" ? "error" : "muted"} title={(d.slip_reasons ?? []).join("; ")}>
                      {d.slip_risk === "high" ? "Likely to slip" : "Slip risk"}</div>}
                  <select style={{ marginTop: 4, fontSize: 12 }} value={d.rep_forecast_category ?? ""} title="Your forecast call"
                    onChange={async (e) => { await api(`/api/deals/${d.id}/rep-forecast`, { method: "PUT",
                      body: JSON.stringify({ category: e.target.value }) }); load(); }}>
                    <option value="" disabled>Your call…</option>
                    <option value="commit">commit</option><option value="best_case">best case</option>
                    <option value="pipeline">pipeline</option><option value="omitted">omit</option>
                  </select></td>
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
                    <DealRoom dealId={d.id} accountId={d.account_id} />
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


function DealRoom({ dealId, accountId }: { dealId: number; accountId: number }) {
  const [room, setRoom] = useState<any>(undefined);
  const [cov, setCov] = useState<any>(null);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [msg, setMsg] = useState("");
  const load = () => {
    api(`/api/deals/${dealId}/room`).then(setRoom).catch(() => setRoom(null));
    api(`/api/accounts/${accountId}/coverage`).then(setCov).catch(() => {});
  };
  useEffect(load, [dealId]);

  async function create(generate: boolean) {
    setMsg("Creating the room…");
    try { const { run_id } = await post(`/api/deals/${dealId}/room`, { generate }); await waitForRun(run_id); setMsg(""); load(); }
    catch (e: any) { setMsg(e.message); }
  }
  async function patchRoom(body: object) { await api(`/api/rooms/${room.id}`, { method: "PATCH", body: JSON.stringify(body) }); load(); }

  return (
    <div className="stack" style={{ marginTop: 12 }}>
      {cov && (
        <div className="card">
          <b>Buying group: {cov.score}% covered</b>{cov.single_threaded && <span className="error"> · single-threaded</span>}
          <div className="muted">
            {Object.entries(cov.roles).map(([r, names]: [string, any]) => `${r.replace("_", " ")}: ${names.length ? names.join(", ") : "nobody engaged"}`).join(" · ")}
          </div>
          {cov.never_engaged.length > 0 && <div className="muted">Known but never engaged: {cov.never_engaged.join(", ")}</div>}
        </div>
      )}
      <div className="card stack">
        <b>Buyer room</b>
        {room === null && (
          <>
            <div className="muted">A private page for the buying committee: summary, proposal, case studies, a shared plan they can
              tick, and answers to their questions from your knowledge base. You see who looked at what.</div>
            <div className="row">
              <button className="primary" onClick={() => create(true)}>Create with AI</button>
              <button onClick={() => create(false)}>Create with the standard plan</button>
              {msg && <span className="muted">{msg}</span>}
            </div>
          </>
        )}
        {room && (
          <>
            <div className="row">
              <input readOnly value={room.url} style={{ maxWidth: 460 }} />
              <button onClick={() => navigator.clipboard.writeText(room.url)}>Copy link</button>
              <label className="row" style={{ gap: 6 }}><input type="checkbox" style={{ width: "auto" }} checked={room.allow_pricing}
                onChange={(e) => patchRoom({ allow_pricing: e.target.checked })} /> Assistant may answer pricing questions</label>
            </div>
            <div className="muted">Plan: {room.plan_progress.done}/{room.plan_progress.total} steps done ·
              {room.visitors.length ? ` ${room.visitors.length} visitor(s): ` + room.visitors.map((v: any) => `${v.visitor} (${v.minutes} min)`).join(", ") : " no visits yet"}</div>
            {room.pending_questions.map((q: any) => (
              <div className="card" key={q.id}><b>Waiting for you</b> <span className="muted">({q.visitor})</span>: {q.question}
                <div className="row">
                  <input placeholder="Your answer (shown to the buyer in the room)" value={answers[q.id] ?? ""}
                    onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
                  <button className="primary" disabled={!answers[q.id]} onClick={async () => {
                    await post(`/api/rooms/${room.id}/questions/${q.id}/answer`, { answer: answers[q.id] }); load(); }}>Send answer</button>
                </div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}