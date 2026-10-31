"use client";
import { useEffect, useState } from "react";
import Timeline from "@/app/timeline";
import { api, Approval, post, waitForRun } from "@/lib/api";

/**
 * Compact side panel shown INSIDE a CRM record page (Salesforce, HubSpot, Dynamics):
 *   /embed?crm=salesforce&id=00Q...   or   /embed?email=person@company.com
 */
export default function Embed() {
  const [contactId, setContactId] = useState<number | null>(null);
  const [lead, setLead] = useState<any>(null);
  const [drafts, setDrafts] = useState<Approval[]>([]);
  const [reviewer, setReviewer] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const params = new URLSearchParams();
    for (const k of ["crm", "id", "email"]) if (q.get(k)) params.set(k, q.get(k)!);
    setReviewer(q.get("user") ?? "");
    api(`/api/leads/lookup?${params}`).then((r) => setContactId(r.contact_id)).catch((e) => setMsg(e.message));
  }, []);

  const load = () => {
    if (!contactId) return;
    api(`/api/leads/${contactId}`).then(setLead).catch((e) => setMsg(e.message));
    api<Approval[]>("/api/approvals?status=pending").then((all) => setDrafts(all.filter((a) => a.contact?.id === contactId)));
  };
  useEffect(load, [contactId]);

  async function run(label: string, path: string, body: unknown = {}) {
    setBusy(label);
    setMsg("");
    try {
      const { run_id } = await post(path, body);
      const r = await waitForRun(run_id);
      setMsg(r.status === "failed" ? `Failed: ${r.error}` : `${label} done.`);
    } catch (e: any) { setMsg(e.message); }
    setBusy("");
    load();
  }

  async function decide(a: Approval, action: "approve" | "reject") {
    if (!reviewer) return setMsg("Enter your name first.");
    try {
      await post(`/api/approvals/${a.id}/${action}`, { reviewer });
      load();
    } catch (e: any) { setMsg(e.message); }
  }

  if (msg && !lead) return <p className="muted">{msg}</p>;
  if (!lead) return <p className="muted">Loading…</p>;
  const b = lead.brief;

  return (
    <div className="stack embed" style={{ fontSize: 13 }}>
      <div>
        <b>{lead.name}</b> {lead.tier && <span className={`pill tier-${lead.tier}`}>{lead.tier} · {lead.score}</span>}
        <div className="muted">{lead.title} · {lead.account} · {lead.status}</div>
      </div>
      {lead.experiment_group === "holdout" && <div className="flag warn">Holdout control group: no AI outreach.</div>}
      {msg && <div className="muted">{msg}</div>}

      <div className="row">
        <button disabled={!!busy} onClick={() => run("Research", `/api/leads/${contactId}/research`)}>Research</button>
        <button disabled={!!busy} onClick={() => run("Dossier", `/api/leads/${contactId}/dossier`)}>Person dossier</button>
        <button disabled={!!busy} onClick={() => run("Sequence", `/api/leads/${contactId}/sequence`, { steps: 3 })}>Write sequence</button>
        <button disabled={!!busy} onClick={() => run("Meeting prep", `/api/leads/${contactId}/meeting-prep`)}>Prep meeting</button>
        {busy && <span className="muted">{busy}…</span>}
      </div>

      {b && <div className="card"><b>Angle:</b> {b.recommended_angle}<div className="muted" style={{ marginTop: 4 }}>{b.summary}</div></div>}
      {lead.dossier?.talking_points?.length > 0 && (
        <div className="card"><b>Talking points:</b><ul>{lead.dossier.talking_points.map((t: string) => <li key={t}>{t}</li>)}</ul></div>
      )}

      <b>Drafts waiting ({drafts.length})</b>
      {drafts.length > 0 && (
        <input placeholder="Your name (for the approval record)" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
      )}
      {drafts.map((a) => (
        <div className="card" key={a.id}>
          <div className="muted">step {a.sequence_step}{a.personalisation_score != null && ` · personalisation ${a.personalisation_score}/100`}</div>
          <b>{a.subject}</b>
          <div style={{ whiteSpace: "pre-wrap" }}>{a.body}</div>
          {a.guardrail_flags.map((f, i) => <div key={i} className={`flag ${f.severity}`}>{f.message}</div>)}
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary" onClick={() => decide(a, "approve")}>Approve</button>
            <button className="danger" onClick={() => decide(a, "reject")}>Reject</button>
          </div>
        </div>
      ))}

      <b>Timeline</b>
      <Timeline accountId={lead.account_id} contactId={lead.contact_id} />
    </div>
  );
}
