"use client";
import { use, useEffect, useRef, useState } from "react";
import { API_URL } from "@/lib/api";

const KEY = "revenueos.room.visitor";

async function call(path: string, body?: unknown) {
  const res = await fetch(`${API_URL}${path}`, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? String(data.detail) : `${res.status}`);
  return data;
}

/** The buyer's page: no sign-in, opened through the secret link the rep shares. */
export default function BuyerRoom({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  const base = `/api/public/rooms/${token}`;
  const [room, setRoom] = useState<any>(null);
  const [error, setError] = useState("");
  const [visitor, setVisitor] = useState("");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [section, setSection] = useState("overview");
  const since = useRef(Date.now());

  const load = () => call(base).then(setRoom).catch(() => setError("This room isn't available. Ask your contact for a new link."));
  useEffect(() => { try { setVisitor(localStorage.getItem(KEY) ?? ""); } catch {} load(); }, []);

  // Record time spent per section (helps your contact know what matters to you).
  useEffect(() => {
    since.current = Date.now();
    return () => {
      const seconds = Math.round((Date.now() - since.current) / 1000);
      if (seconds >= 3) call(`${base}/view`, { visitor: visitor || null, section, seconds }).catch(() => {});
    };
  }, [section, visitor]);

  function remember(v: string) { setVisitor(v); try { localStorage.setItem(KEY, v); } catch {} }

  async function tick(stepId: number, done: boolean) {
    await call(`${base}/steps/${stepId}`, { visitor: visitor || null, done }); load();
  }
  async function comment(stepId: number) {
    const text = prompt("Add a comment for the team");
    if (text) { await call(`${base}/steps/${stepId}`, { visitor: visitor || null, comment: text }); load(); }
  }
  async function ask() {
    setAsking(true);
    try { await call(`${base}/ask`, { question, visitor: visitor || null }); setQuestion(""); load(); }
    catch (e: any) { alert(e.message); }
    setAsking(false);
  }

  if (error) return <p style={{ padding: 32 }}>{error}</p>;
  if (!room) return <p className="muted" style={{ padding: 32 }}>Loading…</p>;
  const accent = `#${room.brand_color ?? "3B5BDB"}`;
  const done = room.steps.filter((s: any) => s.status === "done").length;

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "24px 16px" }}>
      <div style={{ borderTop: `4px solid ${accent}`, paddingTop: 16 }}>
        <div className="muted">{room.company}</div>
        <h1>{room.title}</h1>
      </div>
      <div className="row" style={{ margin: "8px 0 16px" }}>
        <input style={{ maxWidth: 300 }} type="email" placeholder="Your work email (so the team knows who's asking)"
          value={visitor} onChange={(e) => remember(e.target.value)} />
      </div>
      <div className="row" style={{ marginBottom: 16 }}>
        {["overview", "plan", "documents", "questions"].map((s) => (
          <button key={s} className={section === s ? "primary" : ""} onClick={() => setSection(s)}>{s[0].toUpperCase() + s.slice(1)}</button>
        ))}
      </div>

      {section === "overview" && (
        <div className="stack">
          {room.welcome_note && <div className="card">{room.welcome_note}</div>}
          <div className="card" style={{ whiteSpace: "pre-wrap" }}>{room.summary}</div>
          <div className="muted">Plan progress: {done} of {room.steps.length} steps done.</div>
          {room.case_studies.length > 0 && <h2>Similar customers</h2>}
          {room.case_studies.map((c: any) => (
            <details className="card" key={c.id}><summary><b>{c.title}</b></summary><div style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{c.content}</div></details>
          ))}
        </div>
      )}

      {section === "plan" && (
        <table>
          <thead><tr><th></th><th>Step</th><th>Who</th><th>By</th><th></th></tr></thead>
          <tbody>{room.steps.map((s: any) => (
            <tr key={s.id}>
              <td><input type="checkbox" style={{ width: "auto" }} checked={s.status === "done"} onChange={(e) => tick(s.id, e.target.checked)} /></td>
              <td style={{ textDecoration: s.status === "done" ? "line-through" : "none" }}>{s.title}
                {s.comment && <div className="muted">💬 {s.comment}</div>}</td>
              <td>{s.owner_side === "us" ? room.company : s.owner_side === "them" ? "You" : "Both"}</td>
              <td>{s.due_date}</td>
              <td><button onClick={() => comment(s.id)}>Comment</button></td>
            </tr>
          ))}</tbody>
        </table>
      )}

      {section === "documents" && (
        <div className="stack">
          {room.documents.map((d: any) => (
            <div className="card row" key={d.id} style={{ justifyContent: "space-between" }}>
              <b>{d.title}</b>
              {d.has_file ? <a className="button" href={`${API_URL}${base}/documents/${d.id}`}>Download</a> : <span className="muted">shared in the meeting</span>}
            </div>
          ))}
          {!room.documents.length && <p className="muted">Documents will appear here.</p>}
        </div>
      )}

      {section === "questions" && (
        <div className="stack">
          <p className="muted">Ask anything. Answers come from our documentation; anything else goes straight to the team.</p>
          <textarea rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. How long does implementation take?" />
          <div><button className="primary" disabled={asking || question.trim().length < 3} onClick={ask}>{asking ? "Asking…" : "Ask"}</button></div>
          {room.questions.slice().reverse().map((q: any, i: number) => (
            <div className="card" key={i}><b>{q.question}</b>
              <div style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>{q.pending ? <span className="muted">The team will answer this shortly.</span> : q.answer}</div></div>
          ))}
        </div>
      )}
    </div>
  );
}
