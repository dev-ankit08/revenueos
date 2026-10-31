"use client";
import { useEffect, useState } from "react";
import { api, Lead, post } from "@/lib/api";

const RATER_KEY = "revenueos.reviewer";

function Rate({ id, onBack }: { id: number; onBack: () => void }) {
  const [rater, setRater] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [i, setI] = useState(0);
  const [comment, setComment] = useState("");

  useEffect(() => { try { setRater(localStorage.getItem(RATER_KEY) ?? ""); } catch {} }, []);
  useEffect(() => {
    if (rater) api(`/api/evaluations/${id}?rater=${encodeURIComponent(rater)}`).then((r) => {
      setItems(r);
      const next = r.findIndex((x: any) => !x.your_choice);
      setI(next < 0 ? r.length : next);
    });
  }, [id, rater]);

  async function choose(choice: string) {
    await post(`/api/evaluations/${id}/items/${items[i].item_id}/rate`, { rater, choice, comment: comment || null });
    setComment("");
    setI(i + 1);
  }

  if (!rater) return (
    <div className="card row">
      <input style={{ maxWidth: 260 }} placeholder="Your name" onBlur={(e) => {
        const v = e.target.value.trim();
        if (v) { setRater(v); try { localStorage.setItem(RATER_KEY, v); } catch {} }
      }} />
      <span className="muted">Enter your name, then rate.</span>
    </div>
  );
  if (i >= items.length) return <div className="card">All {items.length} rated, thank you. <button onClick={onBack}>Back</button></div>;
  const it = items[i];
  return (
    <div className="stack">
      <div className="muted">{i + 1} of {items.length} · to {it.recipient} · Which would you rather send, and which would get a reply?</div>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        {(["a", "b"] as const).map((side) => (
          <div className="card" key={side}>
            <div className="muted">{side.toUpperCase()}</div>
            <b>{it[side].subject}</b>
            <div style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>{it[side].body}</div>
          </div>
        ))}
      </div>
      <input placeholder="Optional: why?" value={comment} onChange={(e) => setComment(e.target.value)} />
      <div className="row">
        <button className="primary" onClick={() => choose("A")}>A is better</button>
        <button className="primary" onClick={() => choose("B")}>B is better</button>
        <button onClick={() => choose("tie")}>About the same</button>
        <button onClick={onBack}>Stop</button>
      </div>
    </div>
  );
}

export default function Evaluate() {
  const [list, setList] = useState<any[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [rating, setRating] = useState<number | null>(null);
  const [results, setResults] = useState<any>(null);
  const [form, setForm] = useState({ name: "", incumbent_label: "Current approach" });
  const [rows, setRows] = useState<{ contact_id: string; subject: string; body: string }[]>([{ contact_id: "", subject: "", body: "" }]);
  const [msg, setMsg] = useState("");

  const load = () => api("/api/evaluations").then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); api<Lead[]>("/api/leads").then(setLeads).catch(() => {}); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await post("/api/evaluations", { ...form, items: rows.filter((x) => x.contact_id && x.body)
        .map((x) => ({ contact_id: Number(x.contact_id), subject: x.subject || null, body: x.body })) });
      setMsg(`Created with ${r.items} pairs${r.skipped_contacts_without_draft.length ? `; ${r.skipped_contacts_without_draft.length} contacts skipped (no RevenueOS draft yet)` : ""}.`);
      setRows([{ contact_id: "", subject: "", body: "" }]);
      load();
    } catch (err: any) { setMsg(err.message); }
  }

  if (rating) return <><h1>Blind test</h1><Rate id={rating} onBack={() => { setRating(null); load(); }} /></>;
  return (
    <>
      <h1>Blind test</h1>
      <p className="sub">Reps compare a RevenueOS draft with the current approach (human-written or another tool's AI) for the same lead, without knowing which is which.</p>
      {msg && <p className="muted">{msg}</p>}
      <table>
        <thead><tr><th>Test</th><th>Pairs</th><th>Ratings</th><th>Raters</th><th></th></tr></thead>
        <tbody>
          {list.map((ev) => (
            <tr key={ev.id}><td>{ev.name}<div className="muted">vs {ev.incumbent_label}</div></td><td>{ev.items}</td><td>{ev.ratings}</td><td>{ev.raters}</td>
              <td className="row"><button className="primary" onClick={() => setRating(ev.id)}>Rate</button>
                <button onClick={async () => setResults(await api(`/api/evaluations/${ev.id}/results`))}>Results</button></td></tr>
          ))}
          {!list.length && <tr><td colSpan={5} className="muted">No tests yet.</td></tr>}
        </tbody>
      </table>
      {results && (
        <div className="card" style={{ marginTop: 12 }}>
          <b>{results.name}</b>: RevenueOS {results.revenueos_wins} · {results.incumbent_label} {results.incumbent_wins} · ties {results.ties}
          <p><b>{results.verdict}</b></p>
          {results.comments.length > 0 && <ul>{results.comments.map((c: any, i: number) => <li key={i}>{c.rater} preferred {c.preferred}: {c.comment}</li>)}</ul>}
        </div>
      )}

      <h2>New blind test</h2>
      <form className="card stack" onSubmit={create}>
        <div className="row">
          <input style={{ maxWidth: 300 }} required placeholder="Name, e.g. 'October outbound vs our SDR templates'" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input style={{ maxWidth: 260 }} placeholder="What it's compared with" value={form.incumbent_label} onChange={(e) => setForm({ ...form, incumbent_label: e.target.value })} />
        </div>
        <div className="muted">For each lead that already has a RevenueOS draft, paste the email your team (or the other tool) would send.</div>
        {rows.map((r, idx) => (
          <div className="card stack" key={idx}>
            <select value={r.contact_id} onChange={(e) => setRows(rows.map((x, j) => j === idx ? { ...x, contact_id: e.target.value } : x))}>
              <option value="">Lead…</option>
              {leads.map((l) => <option key={l.contact_id} value={l.contact_id}>{l.name} · {l.account}</option>)}
            </select>
            <input placeholder="Their subject" value={r.subject} onChange={(e) => setRows(rows.map((x, j) => j === idx ? { ...x, subject: e.target.value } : x))} />
            <textarea rows={4} placeholder="Their email" value={r.body} onChange={(e) => setRows(rows.map((x, j) => j === idx ? { ...x, body: e.target.value } : x))} />
          </div>
        ))}
        <div className="row">
          <button type="button" onClick={() => setRows([...rows, { contact_id: "", subject: "", body: "" }])}>Add another lead</button>
          <button className="primary">Create test</button>
        </div>
      </form>
    </>
  );
}
