"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, post, waitForRun } from "@/lib/api";

const CONTENT_TYPES = ["linkedin_post", "blog_post", "ad", "landing_page", "newsletter", "case_study", "one_pager", "social_post"];

function CampaignDetail({ id, onChange }: { id: number; onChange: () => void }) {
  const [c, setC] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const load = () => api(`/api/campaigns/${id}`).then(setC).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, [id]);

  async function generate(indexes?: number[]) {
    setBusy(indexes ? `item ${indexes[0] + 1}` : "all items");
    try {
      const { run_id } = await post(`/api/campaigns/${id}/content`, { item_indexes: indexes ?? null });
      const r = await waitForRun(run_id);
      setMsg(r.status === "failed" ? `Failed: ${r.error}` : r.output ?? "Done");
    } catch (e: any) { setMsg(e.message); }
    setBusy("");
    load();
    onChange();
  }

  async function setStatus(status: string) {
    await api(`/api/campaigns/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    load();
    onChange();
  }

  if (!c) return <p className="muted">Loading…</p>;
  const p = c.plan ?? {};
  return (
    <div className="card stack" style={{ marginTop: 12 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div><b>{c.name}</b> <span className="muted">· {c.members} contacts targeted{(c.sf_id || c.crm_id) && " · in CRM"}</span></div>
        <div className="row">
          <select value={c.status} onChange={(e) => setStatus(e.target.value)} style={{ width: "auto" }}>
            {["draft", "active", "completed"].map((s) => <option key={s}>{s}</option>)}
          </select>
          <Link className="btn" href={`/approvals?kind=content`}>Review content ({c.content.pending ?? 0})</Link>
        </div>
      </div>
      <div><b>Objective:</b> {p.objective}</div>
      <div><b>Key message:</b> {p.key_message}</div>
      <div><b>Offer:</b> {p.offer}</div>
      <div className="grid">
        <div><b>KPIs</b><ul>{p.kpis?.map((k: any) => <li key={k.metric}>{k.metric}: {k.target}</li>)}</ul></div>
        <div><b>Channels</b><ul>{p.channels?.map((ch: any) => <li key={ch.channel}>{ch.channel}: {ch.budget_pct}%</li>)}</ul></div>
        <div><b>Audience</b><ul>
          <li>{p.audience?.industries?.join(", ") || "any industry"}</li>
          <li>{p.audience?.title_keywords?.join(", ") || "any title"}</li>
          <li>{p.audience?.regions?.join(", ") || "any region"}</li>
        </ul></div>
      </div>
      <div><b>Sales play:</b> {p.sales_play}</div>
      <h2 style={{ margin: "8px 0 0" }}>Content plan</h2>
      <table>
        <thead><tr><th>Week</th><th>Piece</th><th>Channel</th><th></th></tr></thead>
        <tbody>
          {p.content_plan?.map((item: any, i: number) => (
            <tr key={i}>
              <td>{item.week}</td>
              <td><span className="pill">{item.content_type}</span> {item.title}<div className="muted">{item.angle}</div></td>
              <td>{item.channel}</td>
              <td><button disabled={!!busy} onClick={() => generate([i])}>Draft</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button className="primary" disabled={!!busy} onClick={() => generate()}>Draft all content</button>
        {busy && <span className="muted">Drafting {busy}…</span>}
        {msg && !busy && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}

export default function Campaigns() {
  const [list, setList] = useState<any[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [form, setForm] = useState({ goal: "", budget: "", start_date: "", end_date: "" });
  const [content, setContent] = useState({ content_type: "linkedin_post", brief: "" });
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api("/api/campaigns").then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function run(label: string, path: string, body: unknown, after?: (out: string) => void) {
    setBusy(label);
    setMsg("");
    try {
      const { run_id } = await post(path, body);
      const r = await waitForRun(run_id);
      setMsg(r.status === "failed" ? `Failed: ${r.error}` : r.output ?? "Done");
      if (r.status !== "failed") after?.(r.output ?? "");
    } catch (e: any) { setMsg(e.message); }
    setBusy("");
    load();
  }

  return (
    <>
      <h1>Campaigns</h1>
      <p className="sub">Describe the outcome you want; the Campaign Strategist plans it and Content Studio drafts the pieces into Approvals.</p>

      <form className="card stack" onSubmit={(e) => {
        e.preventDefault();
        run("Planning campaign", "/api/campaigns", {
          goal: form.goal, budget: form.budget ? Number(form.budget) : null,
          start_date: form.start_date || null, end_date: form.end_date || null,
        }, (out) => { const m = out.match(/campaign:(\d+)/); if (m) setOpen(Number(m[1])); });
      }}>
        <textarea rows={3} required minLength={10} placeholder='Goal, e.g. "30 meetings with manufacturing CIOs in Q1 for our data assessment offer"'
          value={form.goal} onChange={(e) => setForm({ ...form, goal: e.target.value })} />
        <div className="row">
          <input style={{ flex: 1, minWidth: 120 }} placeholder="Budget" inputMode="numeric" value={form.budget} onChange={(e) => setForm({ ...form, budget: e.target.value })} />
          <input style={{ flex: 1, minWidth: 140 }} type="date" value={form.start_date} onChange={(e) => setForm({ ...form, start_date: e.target.value })} />
          <input style={{ flex: 1, minWidth: 140 }} type="date" value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} />
          <button className="primary" disabled={!!busy}>Plan campaign</button>
        </div>
      </form>

      <form className="card row" style={{ marginTop: 12 }} onSubmit={(e) => {
        e.preventDefault();
        run("Drafting content", "/api/content", content);
      }}>
        <select style={{ width: 170 }} value={content.content_type} onChange={(e) => setContent({ ...content, content_type: e.target.value })}>
          {CONTENT_TYPES.map((t) => <option key={t}>{t}</option>)}
        </select>
        <input style={{ flex: 1, minWidth: 220 }} required placeholder="One-off piece: what it should say and for whom"
          value={content.brief} onChange={(e) => setContent({ ...content, brief: e.target.value })} />
        <button disabled={!!busy}>Draft</button>
      </form>
      {(busy || msg) && <p className="muted">{busy ? `${busy}… (1-3 min)` : msg}</p>}

      <h2>All campaigns</h2>
      <table>
        <thead><tr><th>Campaign</th><th>Status</th><th>Contacts</th><th>Content</th><th>Budget</th></tr></thead>
        <tbody>
          {list.map((c) => (
            <tr key={c.id}>
              <td><a href="#" onClick={(e) => { e.preventDefault(); setOpen(open === c.id ? null : c.id); }}><b>{c.name}</b></a>
                <div className="muted">{c.goal.slice(0, 120)}</div></td>
              <td>{c.status}</td>
              <td>{c.members}</td>
              <td>{Object.entries(c.content).map(([k, v]) => `${v} ${k}`).join(", ") || "–"}</td>
              <td>{c.budget ? c.budget.toLocaleString() : "–"}</td>
            </tr>
          ))}
          {!list.length && <tr><td colSpan={5} className="muted">No campaigns yet.</td></tr>}
        </tbody>
      </table>
      {open && <CampaignDetail id={open} onChange={load} />}
    </>
  );
}
