"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const LABEL: Record<string, string> = { call: "Call", linkedin: "LinkedIn", email: "Email", follow_up: "Follow-up", prep: "Prep", other: "Task" };

export default function Tasks() {
  const [items, setItems] = useState<any[]>([]);
  const [view, setView] = useState("today");
  const [error, setError] = useState("");

  const load = () => {
    const q = view === "done" ? "status=done" : view === "today" ? "status=open&due=today" : "status=open&due=scheduled";
    api<any[]>(`/api/tasks?${q}`).then(setItems).catch((e) => setError(e.message));
  };
  useEffect(load, [view]);

  async function finish(id: number, status: "done" | "skipped") {
    const outcome = status === "done" ? prompt("Outcome (optional), e.g. 'left voicemail', 'booked a call'") ?? "" : "";
    try {
      await api(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify({ status, outcome: outcome || undefined }) });
      load();
    } catch (e: any) { setError(e.message); }
  }

  const now = Date.now();
  return (
    <>
      <h1>Tasks</h1>
      <p className="sub">Calls and LinkedIn messages your sequences have scheduled, plus next steps from the assistants.</p>
      <div className="row" style={{ marginBottom: 16 }}>
        <select style={{ maxWidth: 200 }} value={view} onChange={(e) => setView(e.target.value)}>
          <option value="today">Due today and overdue</option>
          <option value="upcoming">All open, scheduled</option>
          <option value="done">Done</option>
        </select>
        <span className="muted">{items.length} tasks</span>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="stack">
        {items.map((t) => {
          const overdue = t.status === "open" && t.due_at && new Date(t.due_at).getTime() < now - 86400000;
          return (
            <div className="card stack" key={t.id}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <div>
                  <span className="pill">{LABEL[t.type] ?? t.type}</span> <b>{t.title}</b>
                  {t.contact && <> · <Link href={`/pipeline/${t.contact.id}`}>{t.contact.name}</Link> <span className="muted">({t.contact.account})</span></>}
                </div>
                <div className={overdue ? "error" : "muted"}>
                  {t.due_at ? `${overdue ? "Overdue · " : ""}due ${new Date(t.due_at).toLocaleString()}` : ""}
                </div>
              </div>
              {t.notes && <div style={{ whiteSpace: "pre-wrap" }}>{t.notes}</div>}
              {t.type === "linkedin" && t.contact?.linkedin_url &&
                <a href={t.contact.linkedin_url} target="_blank" rel="noreferrer">Open their LinkedIn profile</a>}
              {t.outcome && <div className="muted">Outcome: {t.outcome}</div>}
              {t.status === "open" && (
                <div className="row">
                  {t.type === "linkedin" && <button onClick={() => navigator.clipboard.writeText(t.notes ?? "")}>Copy message</button>}
                  <button className="primary" onClick={() => finish(t.id, "done")}>Done</button>
                  <button onClick={() => finish(t.id, "skipped")}>Skip</button>
                </div>
              )}
            </div>
          );
        })}
        {!items.length && <p className="muted">Nothing here.</p>}
      </div>
    </>
  );
}
