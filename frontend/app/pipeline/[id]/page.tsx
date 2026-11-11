"use client";
import { use, useEffect, useState } from "react";
import Documents from "@/app/documents";
import Sequence from "@/app/sequence";
import LeadData from "@/app/leaddata";
import Timeline from "@/app/timeline";
import { api, post, SalesDoc, waitForRun } from "@/lib/api";

type Panel = "prep" | "call" | "proposal" | null;

/** Meeting prep, call analysis and proposal actions for one lead. */
function DealActions({ id, accountId, onDone }: { id: string; accountId: number; onDone: () => void }) {
  const [panel, setPanel] = useState<Panel>(null);
  const [prep, setPrep] = useState({ when: "", attendees: "", goal: "" });
  const [transcript, setTranscript] = useState("");
  const [instructions, setInstructions] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  async function run(label: string, path: string, body: unknown) {
    setBusy(label);
    setMsg("");
    try {
      const { run_id } = await post(path.startsWith("/") ? path : `/api/leads/${id}/${path}`, body);
      const r = await waitForRun(run_id);
      setMsg(r.status === "failed" ? `Failed: ${r.error}` : `${label} done.`);
      if (r.status !== "failed") setPanel(null);
    } catch (e: any) { setMsg(e.message); }
    setBusy("");
    onDone();
  }

  return (
    <div className="card stack" style={{ marginTop: 16 }}>
      <div className="row">
        <button onClick={() => setPanel(panel === "prep" ? null : "prep")}>Prep a meeting</button>
        <button onClick={() => setPanel(panel === "call" ? null : "call")}>Analyse a call</button>
        <button onClick={() => setPanel(panel === "proposal" ? null : "proposal")}>Build proposal + deck</button>
        <button disabled={!!busy} onClick={() => run("Person dossier", "dossier", {})}>Research person</button>
        <button disabled={!!busy} title="One account narrative; a role-specific, staggered sequence for every contact here"
          onClick={() => run("Committee outreach", `/api/accounts/${accountId}/committee`, {})}>Committee outreach</button>
        <button disabled={!!busy} title="Personalized landing page, ads, one-pager and an email per contact at this account"
          onClick={() => run("ABM pack", `/api/accounts/${accountId}/abm`, {})}>ABM pack for account</button>
        {busy && <span className="muted">{busy}… (agents at work, 1-3 min)</span>}
        {msg && !busy && <span className="muted">{msg}</span>}
      </div>
      {panel === "prep" && (
        <div className="stack">
          <input placeholder="When (e.g. Tue 3pm CET)" value={prep.when} onChange={(e) => setPrep({ ...prep, when: e.target.value })} />
          <input placeholder="Attendees and roles" value={prep.attendees} onChange={(e) => setPrep({ ...prep, attendees: e.target.value })} />
          <input placeholder="Your goal for the call" value={prep.goal} onChange={(e) => setPrep({ ...prep, goal: e.target.value })} />
          <div><button className="primary" disabled={!!busy} onClick={() => run("Meeting prep", "meeting-prep", prep)}>Write prep brief</button></div>
        </div>
      )}
      {panel === "call" && (
        <div className="stack">
          <textarea rows={10} placeholder="Paste the call transcript or your detailed notes (at least a few sentences)"
            value={transcript} onChange={(e) => setTranscript(e.target.value)} />
          <div className="muted">Updates the deal (MEDDICC, stage, health), syncs the CRM, and drafts a follow-up email into Approvals.</div>
          <div><button className="primary" disabled={!!busy || transcript.length < 50}
            onClick={() => run("Call analysis", "call-analysis", { transcript })}>Analyse call</button></div>
        </div>
      )}
      {panel === "proposal" && (
        <div className="stack">
          <textarea rows={3} placeholder="Optional: scope, emphasis, commercial guidance"
            value={instructions} onChange={(e) => setInstructions(e.target.value)} />
          <div className="muted">Uses the account brief, call summaries and knowledge base. Produces an editable .docx and .pptx.</div>
          <div><button className="primary" disabled={!!busy}
            onClick={() => run("Proposal", "proposal", { instructions: instructions || null })}>Build</button></div>
        </div>
      )}
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  if (!items?.length) return null;
  return (
    <>
      <h2>{title}</h2>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </>
  );
}

export default function LeadDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [lead, setLead] = useState<any>(null);
  const [docs, setDocs] = useState<SalesDoc[]>([]);
  const [error, setError] = useState("");
  const load = () => {
    api(`/api/leads/${id}`).then(setLead).catch((e) => setError(e.message));
    api<SalesDoc[]>(`/api/documents?contact_id=${id}`).then(setDocs).catch(() => {});
  };
  useEffect(load, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!lead) return <p className="muted">Loading…</p>;
  const b = lead.brief;
  const a = lead.account_detail;

  return (
    <>
      <h1>{lead.name}</h1>
      <p className="sub">
        {lead.title} · {a.name} {a.industry && `· ${a.industry}`} {a.employees && `· ${a.employees.toLocaleString()} employees`}
        {" · "}status <b>{lead.status}</b>
        {lead.score !== null && <> · score <b>{lead.score}</b> <span className={`pill tier-${lead.tier}`}>{lead.tier}</span></>}
      </p>
      {lead.experiment_group === "holdout" && (
        <div className="flag warn">
          In the <b>holdout control group</b>: no AI outreach, so we can measure what the outreach adds.{" "}
          <button onClick={async () => { await post(`/api/leads/${id}/release-holdout`); load(); }}>Release</button>
        </div>
      )}
      {lead.score_reason && <div className="card"><b>Why this score:</b> {lead.score_reason}</div>}
      {lead.dossier && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="muted">Person dossier · {new Date(lead.dossier.updated_at).toLocaleDateString()}</div>
          <p>{lead.dossier.summary}</p>
          {lead.dossier.public_statements?.length > 0 && (
            <ul>{lead.dossier.public_statements.map((s: any, i: number) => (
              <li key={i}>{s.statement} <span className="muted">{s.date}</span> <a href={s.source} target="_blank" rel="noreferrer">source</a></li>
            ))}</ul>
          )}
          {lead.dossier.talking_points?.length > 0 && <div><b>Talking points:</b> {lead.dossier.talking_points.join(" · ")}</div>}
        </div>
      )}
      {lead.inbound_message && <div className="card" style={{ marginTop: 12 }}><b>Their message:</b> {lead.inbound_message}</div>}

      <DealActions id={id} accountId={lead.account_id} onDone={load} />

      <h2>Customer timeline</h2>
      <Timeline accountId={lead.account_id} contactId={lead.contact_id} />

      <h2>Documents</h2>
      <Documents docs={docs} />

      {b ? (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="muted">Account brief · {new Date(b.created_at).toLocaleDateString()}</div>
          <p>{b.summary}</p>
          <p><b>Recommended angle:</b> {b.recommended_angle}</p>
          <List title="Priorities" items={b.priorities} />
          <List title="Likely pain points" items={b.pain_points} />
          <List title="Recent news" items={b.recent_news} />
          <List title="Relevant proof points" items={b.relevant_proof_points} />
          <List title="Likely objections" items={b.likely_objections} />
          {b.sources?.length > 0 && (
            <>
              <h2>Sources</h2>
              <ul>{b.sources.map((s: string) => <li key={s}><a href={s} target="_blank" rel="noreferrer">{s}</a></li>)}</ul>
            </>
          )}
        </div>
      ) : <p className="muted" style={{ marginTop: 16 }}>No research brief yet. Use Research on the Pipeline page.</p>}

      <LeadData id={lead.contact_id} lead={lead} onDone={load} />

      <h2>Sequence</h2>
      <Sequence contactId={lead.contact_id} timezone={lead.timezone} />

      <h2>All drafts</h2>
      <table>
        <thead><tr><th>Step</th><th>Message</th><th>Status</th></tr></thead>
        <tbody>
          {lead.approvals.map((x: any) => (
            <tr key={x.id}>
              <td>{x.sequence_step}{x.variant === "B" && "B"} · day {x.send_after_days}</td>
              <td><b>{x.subject}</b><div style={{ whiteSpace: "pre-wrap" }}>{x.body}</div></td>
              <td>{x.status}</td>
            </tr>
          ))}
          {!lead.approvals.length && <tr><td colSpan={3} className="muted">No drafts yet.</td></tr>}
        </tbody>
      </table>

      <h2>Activity</h2>
      <ul>{lead.activities.map((x: any, i: number) => (
        <li key={i}><span className="muted">{new Date(x.at).toLocaleString()}</span> · {x.type}: {x.detail}</li>
      ))}</ul>
    </>
  );
}
