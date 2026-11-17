"use client";
import { useEffect, useState } from "react";
import { api, post, waitForRun } from "@/lib/api";

type Doc = { id: number; title: string; category: string; content: string; chunks: number };

const HELP: Record<string, string> = {
  offering: "What you sell and the outcomes it delivers",
  case_study: "Approved customer stories with real numbers (agents may only cite these)",
  icp: "Ideal customer profile: industries, sizes, regions, buying triggers",
  persona: "Buyer personas and what each cares about",
  brand_voice: "How your company writes",
  competitor: "Competitive positioning",
  pricing: "Engagement models and what reps may say about price",
  objection: "Objections and approved responses",
  rules: "One rule per line: BANNED: <phrase>, REQUIRED: <wording, e.g. a disclaimer>, TONE: <instruction>",
  playbook: "Learned automatically from your results",
  other: "Anything else",
};

export default function Knowledge() {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [cats, setCats] = useState<string[]>([]);
  const [form, setForm] = useState({ title: "", category: "offering", content: "" });
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const load = () => api("/api/kb").then((d) => { setDocs(d.documents); setCats(d.categories); }).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    try {
      await post("/api/kb", form);
      setForm({ ...form, title: "", content: "" });
      setMsg("Saved. Agents will use it on their next run.");
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  async function upload(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("category", form.category);
    try {
      await api("/api/kb/upload", { method: "POST", body: fd });
      setMsg(`Uploaded ${file.name}.`);
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  async function remove(id: number) {
    if (!confirm("Delete this document?")) return;
    await api(`/api/kb/${id}`, { method: "DELETE" });
    load();
  }

  return (
    <>
      <h1>Knowledge base</h1>
      <p className="sub">Everything the agents know about your company. Outreach may only cite facts from here or from account research.</p>

      <KbDrafts onChange={load} />

      <form className="card stack" onSubmit={add}>
        <div className="row">
          <input style={{ flex: 2, minWidth: 200 }} placeholder="Title" required value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <select style={{ flex: 1, minWidth: 160 }} value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
            {cats.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}
          </select>
        </div>
        <div className="muted">{HELP[form.category]}</div>
        <textarea rows={8} placeholder="Paste the content" required value={form.content}
          onChange={(e) => setForm({ ...form, content: e.target.value })} />
        <div className="row">
          <button className="primary">Save document</button>
          <label className="btn">Upload .md / .txt
            <input type="file" accept=".md,.txt" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          </label>
          {msg && <span className="muted">{msg}</span>}
        </div>
      </form>

      {cats.filter((c) => docs.some((d) => d.category === c)).map((c) => (
        <div key={c}>
          <h2>{c.replace("_", " ")}</h2>
          <div className="stack">
            {docs.filter((d) => d.category === c).map((d) => (
              <div className="card" key={d.id}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <a href="#" onClick={(e) => { e.preventDefault(); setOpen(open === d.id ? null : d.id); }}><b>{d.title}</b></a>
                  <button className="danger" onClick={() => remove(d.id)}>Delete</button>
                </div>
                {open === d.id && <div style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{d.content}</div>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}


function KbDrafts({ onChange }: { onChange: () => void }) {
  const [drafts, setDrafts] = useState<any[]>([]);
  const [url, setUrl] = useState("");
  const [msg, setMsg] = useState("");
  const load = () => api<any[]>("/api/kb/drafts").then(setDrafts).catch(() => {});
  useEffect(() => { load(); }, []);

  async function build() {
    setMsg("Reading your website; this takes 2-4 minutes…");
    try { const { run_id } = await post("/api/onboarding/kb-from-website", { url }); const r = await waitForRun(run_id); setMsg(r.output ?? r.error ?? "Done"); load(); }
    catch (e: any) { setMsg(e.message); }
  }
  async function decide(id: number, action: string) { await post(`/api/kb/drafts/${id}/${action}`, {}); load(); onChange(); }
  async function importSequences(file: File) {
    const fd = new FormData(); fd.append("file", file);
    try { const r = await api("/api/onboarding/import-sequences", { method: "POST", body: fd }); setMsg(`Imported ${r.steps_imported} sequence steps as a playbook.`); onChange(); }
    catch (e: any) { setMsg(e.message); }
  }

  return (
    <div className="card stack" style={{ marginBottom: 16 }}>
      <b>Start from your website</b>
      <div className="muted">RevenueOS reads your site and drafts your offering, ideal customers, personas, brand voice and case studies,
        each citing the pages it came from. Nothing goes live until you approve it.</div>
      <div className="row">
        <input style={{ maxWidth: 320 }} placeholder="https://www.yourcompany.com" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button className="primary" disabled={!url} onClick={build}>Build drafts</button>
        <label className="btn">Import sequences CSV (Outreach, Salesloft, HubSpot)
          <input type="file" accept=".csv" hidden onChange={(e) => e.target.files?.[0] && importSequences(e.target.files[0])} /></label>
        {msg && <span className="muted">{msg}</span>}
      </div>
      {drafts.map((d) => (
        <details className="card" key={d.id}>
          <summary><span className="pill">{d.category.replace("_", " ")}</span> <b>{d.title}</b></summary>
          <div style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{d.content}</div>
          <div className="muted">Sources: {(d.source_urls ?? []).join(", ")}</div>
          <div className="row"><button className="primary" onClick={() => decide(d.id, "approve")}>Approve</button>
            <button className="danger" onClick={() => decide(d.id, "reject")}>Reject</button></div>
        </details>
      ))}
    </div>
  );
}