"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, Lead, post, waitForRun } from "@/lib/api";

const EMPTY = { first_name: "", last_name: "", title: "", email: "", company: "", domain: "", industry: "", employees: "" };

export default function Pipeline() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [busy, setBusy] = useState<Record<number, string>>({});
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState(EMPTY);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const load = () => api<Lead[]>("/api/leads").then(setLeads).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function act(id: number, action: "score" | "research" | "sequence") {
    setBusy((b) => ({ ...b, [id]: action }));
    try {
      const { run_id } = await post(`/api/leads/${id}/${action}`, action === "sequence" ? { steps: 3 } : undefined);
      const run = await waitForRun(run_id);
      setMsg(run.status === "failed" ? `Failed: ${run.error}` : `${action} finished for lead ${id}.`);
    } catch (e: any) { setMsg(e.message); }
    setBusy((b) => { const n = { ...b }; delete n[id]; return n; });
    load();
  }

  async function batch() {
    const ids = [...selected];
    setMsg(`Running outbound workflow on ${ids.length} leads (score → research → sequence)…`);
    try {
      const { run_id } = await post("/api/leads/batch-outbound", { contact_ids: ids, steps: 3 });
      const run = await waitForRun(run_id);
      setMsg(run.output ?? run.error ?? "Done");
    } catch (e: any) { setMsg(e.message); }
    setSelected(new Set());
    load();
  }

  async function addLead(e: React.FormEvent) {
    e.preventDefault();
    try {
      await post("/api/leads", { ...form, employees: form.employees ? Number(form.employees) : null,
        title: form.title || null, email: form.email || null, domain: form.domain || null, industry: form.industry || null });
      setForm(EMPTY);
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  async function importCrm() {
    try {
      const r = await post("/api/crm/import");
      setMsg(`Imported ${r.imported} leads from ${r.crm}.`);
    } catch (e: any) { setMsg(e.message); }
    load();
  }

  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <>
      <h1>Pipeline</h1>
      <p className="sub">Score, research and sequence leads. Drafts land in Approvals.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={importCrm}>Import open leads from CRM</button>
        <button className="primary" disabled={!selected.size} onClick={batch}>Run outbound on {selected.size || ""} selected</button>
        {msg && <span className="muted">{msg}</span>}
      </div>
      <table>
        <thead><tr><th></th><th>Lead</th><th>Company</th><th>Score</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {leads.map((l) => (
            <tr key={l.contact_id}>
              <td><input type="checkbox" style={{ width: "auto" }} checked={selected.has(l.contact_id)} onChange={() => toggle(l.contact_id)} /></td>
              <td><Link href={`/pipeline/${l.contact_id}`}>{l.name}</Link><div className="muted">{l.title}</div></td>
              <td>{l.account}{l.has_brief && <div><span className="pill">brief ready</span></div>}</td>
              <td>{l.score ?? "–"} {l.tier && <span className={`pill tier-${l.tier}`}>{l.tier}</span>}</td>
              <td>{l.status}<div className="muted">{l.source}</div></td>
              <td>
                {busy[l.contact_id] ? <span className="muted">{busy[l.contact_id]}…</span> : (
                  <div className="row">
                    <button onClick={() => act(l.contact_id, "score")}>Score</button>
                    <button onClick={() => act(l.contact_id, "research")}>Research</button>
                    <button onClick={() => act(l.contact_id, "sequence")}>Write sequence</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {!leads.length && <tr><td colSpan={6} className="muted">No leads yet. Add one below, import from CRM, or run the seed script.</td></tr>}
        </tbody>
      </table>

      <h2>Add a lead</h2>
      <form className="card grid" onSubmit={addLead}>
        {(Object.keys(EMPTY) as (keyof typeof EMPTY)[]).map((k) => (
          <input key={k} placeholder={k.replace("_", " ")} value={form[k]} required={["first_name", "last_name", "company"].includes(k)}
            onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
        ))}
        <button className="primary">Add lead</button>
      </form>
    </>
  );
}
