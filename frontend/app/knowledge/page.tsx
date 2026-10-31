"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

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
