"use client";
import { useEffect, useState } from "react";
import { api, Approval, post } from "@/lib/api";

const REVIEWER_KEY = "revenueos.reviewer";

function Draft({ a, reviewer, onDone }: { a: Approval; reviewer: string; onDone: () => void }) {
  const [subject, setSubject] = useState(a.subject ?? "");
  const [body, setBody] = useState(a.body);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const blocked = a.guardrail_flags.some((f) => f.severity === "block");

  async function decide(action: "approve" | "reject") {
    if (!reviewer) return setError("Enter your name or email at the top first.");
    setBusy(true);
    setError("");
    try {
      await post(`/api/approvals/${a.id}/${action}`, action === "approve" ? { reviewer, subject, body } : { reviewer });
      onDone();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  }

  return (
    <div className="card stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        {a.kind === "content" ? (
          <div><span className="pill">{a.content_type?.replace("_", " ")}</span> <b>{a.title}</b>
            <span className="muted"> · by {a.created_by_agent}</span></div>
        ) : (
          <>
            <div>
              <b>{a.contact?.name}</b> <span className="muted">{a.contact?.title} · {a.contact?.account}</span>{" "}
              {a.contact?.tier && <span className={`pill tier-${a.contact.tier}`}>{a.contact.tier}</span>}
            </div>
            <div className="muted">
              {a.kind.replace("_", " ")} · step {a.sequence_step}{a.variant === "B" ? " (variant B)" : ""}
              {a.send_after_days > 0 && ` · sends ${a.send_after_days} days after approval`}
            </div>
          </>
        )}
      </div>
      {a.kind === "email" && <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Subject" />}
      <textarea rows={Math.min(14, body.split("\n").length + 2)} value={body} onChange={(e) => setBody(e.target.value)} />
      {a.rationale && <div className="muted"><b>Agent's reasoning:</b> {a.rationale}</div>}
      {a.personalisation_score != null && (
        <div className="muted">
          <b>Personalisation:</b>{" "}
          <span className={`pill ${a.personalisation_score >= 80 ? "tier-A" : a.personalisation_score >= 60 ? "tier-B" : "tier-C"}`}>
            {a.personalisation_score}/100</span>
          {(a.personalisation_notes?.specific_facts_used ?? []).length > 0 &&
            <> · uses: {(a.personalisation_notes?.specific_facts_used ?? []).join("; ")}</>}
        </div>
      )}
      {(a.claims ?? []).length > 0 && (
        <div className="muted"><b>Sources:</b>
          <ul style={{ margin: "4px 0 0" }}>{(a.claims ?? []).map((c, i) => (
            <li key={i}>{c.text}: {c.source.startsWith("http")
              ? <a href={c.source} target="_blank" rel="noreferrer">{c.source}</a> : c.source}</li>
          ))}</ul>
        </div>
      )}
      {a.guardrail_flags.map((f, i) => <div key={i} className={`flag ${f.severity}`}>{f.message}</div>)}
      {error && <div className="error">{error}</div>}
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => decide("approve")}
          title={blocked ? "Fix the blocking issue in the text, then approve" : undefined}>
          {a.kind === "content" ? "Approve for publishing" : a.kind !== "email" ? "Approve (copy & send manually)" : a.send_after_days > 0 ? "Approve & schedule" : "Approve & send"}
        </button>
        <button className="danger" disabled={busy} onClick={() => decide("reject")}>Reject</button>
        {a.kind !== "email" && <button onClick={() => navigator.clipboard.writeText(body)}>Copy text</button>}
      </div>
    </div>
  );
}

export default function Approvals() {
  const [items, setItems] = useState<Approval[]>([]);
  const [status, setStatus] = useState("pending");
  const [kind, setKind] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    try { setReviewer(localStorage.getItem(REVIEWER_KEY) ?? ""); } catch {}
    setKind(new URLSearchParams(window.location.search).get("kind") ?? "");
  }, []);
  const load = () => api<Approval[]>(`/api/approvals?status=${status}${kind ? `&kind=${kind}` : ""}`)
    .then(setItems).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [status, kind]);

  return (
    <>
      <h1>Approvals</h1>
      <p className="sub">Nothing leaves without a human. Edit, then approve or reject. Guardrail warnings appear under each draft.</p>
      <div className="row" style={{ marginBottom: 16 }}>
        <input style={{ maxWidth: 260 }} placeholder="Your name or email" value={reviewer}
          onChange={(e) => { setReviewer(e.target.value); try { localStorage.setItem(REVIEWER_KEY, e.target.value); } catch {} }} />
        <select style={{ maxWidth: 160 }} value={status} onChange={(e) => setStatus(e.target.value)}>
          {["pending", "approved", "sent", "rejected", "failed"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select style={{ maxWidth: 180 }} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">all types</option>
          <option value="email">emails</option>
          <option value="linkedin_message">LinkedIn messages</option>
          <option value="content">marketing content</option>
        </select>
        <span className="muted">{items.length} items</span>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="stack">
        {items.map((a) => status === "pending"
          ? <Draft key={a.id} a={a} reviewer={reviewer} onDone={load} />
          : (
            <div className="card" key={a.id}>
              {a.kind === "content"
                ? <div><span className="pill">{a.content_type}</span> <b>{a.title}</b></div>
                : <><b>{a.contact?.name}</b> <span className="muted">· {a.contact?.account} · step {a.sequence_step}</span>
                  <div><b>{a.subject}</b></div></>}
              <div style={{ whiteSpace: "pre-wrap" }}>{a.body}</div>
              {a.review_note && <div className="muted">{a.review_note}</div>}
            </div>
          ))}
        {!items.length && <p className="muted">Nothing here.</p>}
      </div>
    </>
  );
}
