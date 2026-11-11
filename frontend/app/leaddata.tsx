"use client";
import { useState } from "react";
import { api, post } from "@/lib/api";

/** Owner, funnel stage and data quality for one lead, with enrich / verify actions. */
export default function LeadData({ id, lead, onDone }: { id: number; lead: any; onDone: () => void }) {
  const [owner, setOwner] = useState(lead.owner ?? "");
  const [basis, setBasis] = useState(lead.lawful_basis ?? "");
  const [msg, setMsg] = useState("");

  async function run(label: string, fn: () => Promise<any>) {
    setMsg(`${label}…`);
    try { const r = await fn(); setMsg(r); onDone(); } catch (e: any) { setMsg(e.message); }
  }

  return (
    <div className="card row" style={{ marginTop: 12, justifyContent: "space-between", flexWrap: "wrap" }}>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <span>Stage: <b>{(lead.lifecycle ?? "lead").toUpperCase()}</b></span>
        <span>Email: <b>{lead.email_status ?? "not checked"}</b></span>
        <select style={{ maxWidth: 200 }} value={basis} onChange={(e) => {
          const v = e.target.value; setBasis(v);
          run("Saving lawful basis", async () => {
            const source = v ? prompt("Where does this come from? e.g. 'web form consent 2026-11-02' or 'LIA document'") : null;
            await api(`/api/leads/${id}/lawful-basis`, { method: "PUT", body: JSON.stringify({ lawful_basis: v || null, source }) });
            return "Lawful basis saved.";
          });
        }}>
          <option value="">Lawful basis: none recorded</option>
          <option value="consent">Consent</option>
          <option value="legitimate_interest">Legitimate interest</option>
          <option value="contract">Contract / customer</option>
        </select>
        <input style={{ maxWidth: 220 }} placeholder="Owner (rep email)" value={owner} onChange={(e) => setOwner(e.target.value)} />
        <button onClick={() => run("Saving owner", async () => {
          await api(`/api/leads/${id}/owner`, { method: "PUT", body: JSON.stringify({ owner: owner || null }) });
          return "Owner saved.";
        })}>Save owner</button>
      </div>
      <div className="row">
        <button onClick={() => run("Enriching", async () => {
          const r = await post(`/api/leads/${id}/enrich`);
          return r.filled.length ? `Filled: ${r.filled.join(", ")}` : "Nothing new found.";
        })}>Enrich</button>
        <button onClick={() => run("Verifying", async () => {
          const r = await post(`/api/leads/${id}/verify-email`);
          return `Email ${r.status} (${r.detail})`;
        })}>Verify email</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}
