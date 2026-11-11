"use client";
import { useEffect, useState } from "react";
import { api, Approval, post, TOKEN_KEY } from "@/lib/api";

const REVIEWER_KEY = "revenueos.reviewer";
type Reason = { code: string; label: string };

function Draft({ a, reviewer, needName, reasons, onDone }:
  { a: Approval; reviewer: string; needName: boolean; reasons: Reason[]; onDone: () => void }) {
  const [subject, setSubject] = useState(a.subject ?? "");
  const [body, setBody] = useState(a.body);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const blocked = a.guardrail_flags.some((f) => f.severity === "block");
  const edited = subject !== (a.subject ?? "") || body !== a.body;

  async function decide(action: "approve" | "reject") {
    if (needName && !reviewer) return setError("Enter your name or email at the top first.");
    if (action === "reject" && !reason) return setError("Pick a reason so the assistants can learn from it.");
    setBusy(true);
    setError("");
    try {
      await post(`/api/approvals/${a.id}/${action}`, action === "approve"
        ? { reviewer, subject, body }
        : { reviewer, reason, note: note || undefined });
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
      {rejecting && (
        <div className="row">
          <select style={{ maxWidth: 240 }} value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="">Why reject? (required)</option>
            {reasons.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </select>
          <input style={{ maxWidth: 320 }} placeholder="What should it do differently? (optional)" value={note}
            onChange={(e) => setNote(e.target.value)} />
          <button className="danger" disabled={busy || !reason} onClick={() => decide("reject")}>Confirm reject</button>
          <button disabled={busy} onClick={() => { setRejecting(false); setError(""); }}>Cancel</button>
        </div>
      )}
      {edited && <div className="muted">You've edited this draft. Your changes are kept alongside the original so the assistants can learn from them.</div>}
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => decide("approve")}
          title={blocked ? "Fix the blocking issue in the text, then approve" : undefined}>
          {a.kind === "content" ? "Approve for publishing" : a.kind === "nurture_email" ? "Approve nurture step" : a.kind !== "email" ? "Approve (copy & send manually)" : a.send_after_days > 0 ? "Approve & schedule" : "Approve & send"}
        </button>
        {!rejecting && <button className="danger" disabled={busy} onClick={() => setRejecting(true)}>Reject</button>}
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
  const [needName, setNeedName] = useState(false);
  const [reasons, setReasons] = useState<Reason[]>([]);
  const [error, setError] = useState("");
  const [order, setOrder] = useState("contact");
  const [selected, setSelected] = useState<number[]>([]);
  const [bulkMsg, setBulkMsg] = useState("");

  useEffect(() => {
    // Signed-in users are recorded automatically; the name box is only for installs without accounts.
    try {
      setNeedName(!localStorage.getItem(TOKEN_KEY));
      setReviewer(localStorage.getItem(REVIEWER_KEY) ?? "");
    } catch { setNeedName(true); }
    const params = new URLSearchParams(window.location.search);
    setKind(params.get("kind") ?? "");
    if (params.get("status")) setStatus(params.get("status")!);
    api<Reason[]>("/api/approvals/reject-reasons").then(setReasons).catch(() => {});
  }, []);
  const load = () => api<Approval[]>(`/api/approvals?status=${status}&order=${order}${kind ? `&kind=${kind}` : ""}`)
    .then((x) => { setItems(x); setSelected([]); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [status, kind, order]);

  async function bulk(action: "approve" | "reject") {
    let reason: string | undefined;
    if (action === "reject") {
      reason = prompt(`Reason for rejecting ${selected.length} drafts? One of: ${reasons.map((r) => r.code).join(", ")}`) ?? undefined;
      if (!reason) return;
    } else if (!confirm(`Approve ${selected.length} drafts exactly as written? Emails due now are sent.`)) return;
    try {
      const r = await post("/api/approvals/bulk", { ids: selected, action, reason, reviewer });
      setBulkMsg(`${r.done.length} done${r.skipped.length ? `, ${r.skipped.length} skipped (${r.skipped.map((s: any) => s.why).slice(0, 2).join("; ")})` : ""}.`);
      load();
    } catch (e: any) { setBulkMsg(e.message); }
  }

  return (
    <>
      <h1>Approvals</h1>
      <p className="sub">Nothing leaves without a human. Edit, then approve or reject. Guardrail warnings appear under each draft.</p>
      <div className="row" style={{ marginBottom: 16 }}>
        {needName && <input style={{ maxWidth: 260 }} placeholder="Your name or email" value={reviewer}
          onChange={(e) => { setReviewer(e.target.value); try { localStorage.setItem(REVIEWER_KEY, e.target.value); } catch {} }} />}
        <select style={{ maxWidth: 160 }} value={status} onChange={(e) => setStatus(e.target.value)}>
          {["pending", "approved", "sent", "rejected", "failed"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select style={{ maxWidth: 180 }} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">all types</option>
          <option value="email">emails</option>
          <option value="linkedin_message">LinkedIn messages</option>
          <option value="content">marketing content</option>
          <option value="nurture_email">nurture emails</option>
        </select>
        <select style={{ maxWidth: 170 }} value={order} onChange={(e) => setOrder(e.target.value)}>
          <option value="contact">grouped by person</option>
          <option value="oldest">oldest first</option>
          <option value="newest">newest first</option>
        </select>
        <span className="muted">{items.length} items</span>
      </div>
      {status === "pending" && items.length > 0 && (
        <div className="row" style={{ marginBottom: 12 }}>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" style={{ width: "auto" }}
            checked={selected.length === items.length} onChange={(e) => setSelected(e.target.checked ? items.map((a) => a.id) : [])} />
            Select all</label>
          <button disabled={!selected.length} onClick={() => bulk("approve")}>Approve selected ({selected.length})</button>
          <button className="danger" disabled={!selected.length} onClick={() => bulk("reject")}>Reject selected</button>
          {bulkMsg && <span className="muted">{bulkMsg}</span>}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <div className="stack">
        {items.map((a) => status === "pending"
          ? <div key={a.id} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" style={{ width: "auto", marginTop: 18 }} checked={selected.includes(a.id)}
                onChange={(e) => setSelected(e.target.checked ? [...selected, a.id] : selected.filter((x) => x !== a.id))} />
              <div style={{ flex: 1, minWidth: 0 }}>
                {(a as any).waiting_hours != null && (a as any).waiting_hours > 24 &&
                  <div className="muted" style={{ marginBottom: 4 }}>Waiting {Math.round((a as any).waiting_hours)} hours</div>}
                <Draft a={a} reviewer={reviewer} needName={needName} reasons={reasons} onDone={load} />
              </div>
            </div>
          : (
            <div className="card" key={a.id}>
              {a.kind === "content"
                ? <div><span className="pill">{a.content_type}</span> <b>{a.title}</b></div>
                : <><b>{a.contact?.name}</b> <span className="muted">· {a.contact?.account} · step {a.sequence_step}</span>
                  <div><b>{a.subject}</b></div></>}
              <div style={{ whiteSpace: "pre-wrap" }}>{a.body}</div>
              <div className="muted">
                {a.reviewed_by && <>by {a.reviewed_by}</>}
                {a.edit_pct != null && a.status !== "rejected" && <> · {a.edit_pct === 0 ? "sent as written" : `${a.edit_pct}% edited`}</>}
                {a.reject_reason && <> · reason: {reasons.find((r) => r.code === a.reject_reason)?.label ?? a.reject_reason}</>}
              </div>
              {a.review_note && <div className="muted">{a.review_note}</div>}
              {a.status === "approved" && a.kind === "content" && ["linkedin_post", "social_post"].includes(a.content_type ?? "") && (
                (a as any).published_url
                  ? <div className="muted">Published: <a href={(a as any).published_url} target="_blank" rel="noreferrer">{(a as any).published_url}</a></div>
                  : <button onClick={async () => {
                      try { const r = await post(`/api/approvals/${a.id}/publish`); alert(`Published: ${r.published_url}`); load(); }
                      catch (e: any) { alert(e.message); }
                    }}>Publish to LinkedIn</button>
              )}
            </div>
          ))}
        {!items.length && <p className="muted">Nothing here.</p>}
      </div>
    </>
  );
}
